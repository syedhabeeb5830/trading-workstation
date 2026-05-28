"""
algo/risk_guard.py — Institutional Kill Switch & Risk Management
==================================================================
This module is the LAST gate before any order hits the market.
No order bypasses the risk guard. Period.

Kill switch triggers (hard stop, square off everything):
  1. Daily loss exceeds max_daily_loss_pct
  2. Daily profit exceeds max_daily_profit_pct (greed guard)
  3. Time past square_off_time
  4. Manual kill (Ctrl+C sends square-off)

Circuit breakers (pause, don't kill):
  5. N consecutive losses → pause for M minutes
  6. Max trades per day reached → no new trades

Position limits:
  7. Max concurrent positions
  8. Max risk per trade

Design: the risk guard is STATEFUL — it tracks all trades taken today.
It WRAPS the executor. Nothing reaches the executor without passing here.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Optional
import threading

from algo.config import ALGO_CONFIG
from algo.strategies import Signal, SignalType


@dataclass
class TradeRecord:
    """Record of a single algo trade (open or closed)."""
    symbol: str
    signal_type: SignalType
    entry_price: float
    stop_loss: float
    target: float
    quantity: int
    entry_time: datetime
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    pnl: float = 0.0
    status: str = "OPEN"  # OPEN, WIN, LOSS, SQUAREOFF

    @property
    def is_open(self) -> bool:
        return self.status == "OPEN"

    @property
    def is_long(self) -> bool:
        return self.signal_type == SignalType.BUY

    def close(self, exit_price: float, exit_time: datetime, reason: str = "") -> None:
        self.exit_price = exit_price
        self.exit_time = exit_time
        if self.is_long:
            self.pnl = (exit_price - self.entry_price) * self.quantity
        else:
            self.pnl = (self.entry_price - exit_price) * self.quantity
        self.status = "WIN" if self.pnl > 0 else "LOSS"
        if reason == "SQUAREOFF":
            self.status = "SQUAREOFF"

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "type": self.signal_type.value,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "target": self.target,
            "quantity": self.quantity,
            "entry_time": self.entry_time.isoformat(),
            "exit_price": self.exit_price,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "pnl": round(self.pnl, 2),
            "status": self.status,
        }


class RiskGuardVerdict:
    """Result of a risk guard check."""
    def __init__(self, allowed: bool, reason: str = ""):
        self.allowed = allowed
        self.reason = reason

    def __bool__(self):
        return self.allowed


class RiskGuard:
    """
    Institutional-grade risk management layer.
    Maintains full state of today's algo session.
    """

    def __init__(self, config: dict = None):
        self.cfg = config or ALGO_CONFIG
        self._trades: list[TradeRecord] = []
        self._lock = threading.Lock()
        self._killed = False
        self._paused_until: Optional[datetime] = None
        self._consecutive_losses = 0

        # Parse time configs
        self._square_off = time(*map(int, self.cfg["square_off_time"].split(":")))
        self._no_new_after = time(*map(int, self.cfg["no_new_trades_after"].split(":")))

    # ── State queries ─────────────────────────────────────────────────────────

    @property
    def is_killed(self) -> bool:
        return self._killed

    @property
    def is_paused(self) -> bool:
        if self._paused_until is None:
            return False
        return datetime.now() < self._paused_until

    @property
    def open_positions(self) -> list[TradeRecord]:
        with self._lock:
            return [t for t in self._trades if t.is_open]

    @property
    def closed_trades(self) -> list[TradeRecord]:
        with self._lock:
            return [t for t in self._trades if not t.is_open]

    @property
    def total_trades_today(self) -> int:
        with self._lock:
            return len(self._trades)

    @property
    def daily_pnl(self) -> float:
        """Realized + unrealized P&L for the day."""
        with self._lock:
            return sum(t.pnl for t in self._trades)

    @property
    def realized_pnl(self) -> float:
        with self._lock:
            return sum(t.pnl for t in self._trades if not t.is_open)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.closed_trades if t.status == "WIN")

    @property
    def losses(self) -> int:
        return sum(1 for t in self.closed_trades if t.status == "LOSS")

    # ── Core risk checks ──────────────────────────────────────────────────────

    def check_new_trade(self, signal: Signal, current_time: datetime) -> RiskGuardVerdict:
        """
        Gate check before opening a new position.
        Returns (allowed, reason).
        """
        # ── Hard kills ────────────────────────────────────────────────
        if self._killed:
            return RiskGuardVerdict(False, "KILL SWITCH ACTIVE — session terminated")

        # ── Time check ────────────────────────────────────────────────
        if current_time.time() >= self._no_new_after:
            return RiskGuardVerdict(
                False,
                f"No new trades after {self.cfg['no_new_trades_after']} "
                f"(current: {current_time.strftime('%H:%M')})"
            )

        # ── Pause check ───────────────────────────────────────────────
        if self.is_paused:
            remaining = (self._paused_until - datetime.now()).seconds // 60
            return RiskGuardVerdict(
                False,
                f"PAUSED — {self._consecutive_losses} consecutive losses. "
                f"Resumes in {remaining} min"
            )

        # ── Max trades today ──────────────────────────────────────────
        if self.total_trades_today >= self.cfg["max_trades_per_day"]:
            return RiskGuardVerdict(
                False,
                f"Max trades reached ({self.cfg['max_trades_per_day']}/day)"
            )

        # ── Max concurrent positions ──────────────────────────────────
        if len(self.open_positions) >= self.cfg["max_concurrent_positions"]:
            return RiskGuardVerdict(
                False,
                f"Max concurrent positions ({self.cfg['max_concurrent_positions']})"
            )

        # ── Daily loss limit ──────────────────────────────────────────
        max_loss = self.cfg["capital"] * (self.cfg["max_daily_loss_pct"] / 100)
        if self.realized_pnl <= -max_loss:
            self._kill("Daily loss limit hit")
            return RiskGuardVerdict(False, f"KILL: Daily loss ₹{abs(self.realized_pnl):,.0f} exceeds limit ₹{max_loss:,.0f}")

        # ── Daily profit cap (greed guard) ────────────────────────────
        max_profit = self.cfg["capital"] * (self.cfg["max_daily_profit_pct"] / 100)
        if self.realized_pnl >= max_profit:
            self._kill("Daily profit target reached — greed guard")
            return RiskGuardVerdict(False, f"GREED GUARD: Profit ₹{self.realized_pnl:,.0f} hit target. Stop trading.")

        # ── Per-trade risk check ──────────────────────────────────────
        risk_per_share = signal.risk_per_share
        max_risk = self.cfg["max_loss_per_trade"]
        max_qty = int(max_risk / risk_per_share) if risk_per_share > 0 else 0
        if max_qty <= 0:
            return RiskGuardVerdict(False, f"Risk per share ₹{risk_per_share:.2f} exceeds max trade risk ₹{max_risk}")

        # ── All checks passed ─────────────────────────────────────────
        return RiskGuardVerdict(True)

    def calculate_position_size(self, signal: Signal) -> int:
        """Calculate quantity based on risk per trade."""
        risk_per_share = signal.risk_per_share
        if risk_per_share <= 0:
            return 0

        max_risk = self.cfg["max_loss_per_trade"]
        qty = int(max_risk / risk_per_share)

        # Also cap by leverage-adjusted capital
        capital_available = self.cfg["capital"] * self.cfg["leverage"]
        # Subtract already committed capital
        committed = sum(
            t.entry_price * t.quantity for t in self.open_positions
        )
        available = capital_available - committed
        max_qty_by_capital = int(available / signal.price) if signal.price > 0 else 0

        return min(qty, max_qty_by_capital)

    # ── Trade lifecycle ───────────────────────────────────────────────────────

    def record_entry(self, signal: Signal, quantity: int) -> TradeRecord:
        """Record a new trade entry."""
        trade = TradeRecord(
            symbol=signal.symbol,
            signal_type=signal.type,
            entry_price=signal.price,
            stop_loss=signal.stop_loss,
            target=signal.target,
            quantity=quantity,
            entry_time=signal.timestamp,
        )
        with self._lock:
            self._trades.append(trade)
        return trade

    def record_exit(self, symbol: str, exit_price: float, exit_time: datetime,
                    reason: str = "") -> Optional[TradeRecord]:
        """Record a trade exit. Updates consecutive loss tracking."""
        with self._lock:
            for trade in reversed(self._trades):
                if trade.symbol == symbol and trade.is_open:
                    trade.close(exit_price, exit_time, reason)

                    # Track consecutive losses
                    if trade.status == "LOSS":
                        self._consecutive_losses += 1
                        if self._consecutive_losses >= self.cfg["consecutive_loss_pause"]:
                            self._pause()
                    else:
                        self._consecutive_losses = 0

                    # Check daily loss after exit
                    self._check_daily_limits()
                    return trade
        return None

    def square_off_all(self, prices: dict[str, float], current_time: datetime) -> list[TradeRecord]:
        """
        Square off all open positions. Called at EOD or on kill switch.
        Returns list of closed trades.
        """
        closed = []
        for trade in self.open_positions:
            price = prices.get(trade.symbol, trade.entry_price)
            trade.close(price, current_time, reason="SQUAREOFF")
            closed.append(trade)
            self._consecutive_losses = 0  # reset on square-off
        return closed

    # ── Internal state management ─────────────────────────────────────────────

    def _kill(self, reason: str) -> None:
        """Activate kill switch — no more trading today."""
        self._killed = True
        self._kill_reason = reason

    def _pause(self) -> None:
        """Pause trading for N minutes after consecutive losses."""
        duration = self.cfg["pause_duration_minutes"]
        self._paused_until = datetime.now() + timedelta(minutes=duration)

    def _check_daily_limits(self) -> None:
        """Check if daily limits are breached after any trade exit.
        MUST be called while _lock is already held (does not re-acquire it)."""
        max_loss = self.cfg["capital"] * (self.cfg["max_daily_loss_pct"] / 100)
        # Sum directly — do NOT call self.realized_pnl (that would deadlock since
        # _lock is already held by the caller record_exit/square_off_all).
        pnl = sum(t.pnl for t in self._trades if not t.is_open)
        if pnl <= -max_loss:
            self._kill(f"Daily loss limit: ₹{abs(pnl):,.0f}")

    def should_square_off(self, current_time: datetime) -> bool:
        """Check if it's time for mandatory EOD square-off."""
        return current_time.time() >= self._square_off

    # ── Status & reporting ────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Full risk guard status for display."""
        return {
            "killed": self._killed,
            "kill_reason": getattr(self, "_kill_reason", ""),
            "paused": self.is_paused,
            "paused_until": self._paused_until.isoformat() if self._paused_until else None,
            "consecutive_losses": self._consecutive_losses,
            "total_trades": self.total_trades_today,
            "max_trades": self.cfg["max_trades_per_day"],
            "open_positions": len(self.open_positions),
            "max_positions": self.cfg["max_concurrent_positions"],
            "realized_pnl": round(self.realized_pnl, 2),
            "daily_pnl": round(self.daily_pnl, 2),
            "max_daily_loss": self.cfg["capital"] * (self.cfg["max_daily_loss_pct"] / 100),
            "max_daily_profit": self.cfg["capital"] * (self.cfg["max_daily_profit_pct"] / 100),
        }

    def reset(self) -> None:
        """Reset for a new trading day."""
        with self._lock:
            self._trades.clear()
            self._killed = False
            self._paused_until = None
            self._consecutive_losses = 0
