"""
algo/risk_guard.py — Institutional Risk Management
====================================================
Kill switch, circuit breaker, position sizing, daily limits.

Critical note:
  _check_daily_limits() is always called INSIDE a with self._lock: block.
  It must NOT call any property that also acquires _lock (e.g. realized_pnl)
  as threading.Lock is NOT reentrant — this would deadlock.
"""

from __future__ import annotations
import threading
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Optional

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
    partial_pnl: float = 0.0       # P&L locked by 50% partial exit at 1R (before final close)
    pnl: float = 0.0
    status: str = "OPEN"          # OPEN | WIN | LOSS | SQUAREOFF
    exit_reason: str = ""

    @property
    def is_open(self) -> bool:
        return self.status == "OPEN"

    @property
    def is_long(self) -> bool:
        return self.signal_type == SignalType.BUY

    def close(self, exit_price: float, exit_time: datetime,
               reason: str = "") -> None:
        self.exit_price  = exit_price
        self.exit_time   = exit_time
        self.exit_reason = reason
        if self.is_long:
            self.pnl = self.partial_pnl + (exit_price - self.entry_price) * self.quantity
        else:
            self.pnl = self.partial_pnl + (self.entry_price - exit_price) * self.quantity
        if reason == "SQUAREOFF" or reason == "EOD_SQUAREOFF":
            self.status = "SQUAREOFF"
        else:
            self.status = "WIN" if self.pnl > 0 else "LOSS"

    def to_dict(self) -> dict:
        return {
            "symbol":      self.symbol,
            "type":        self.signal_type.value,
            "entry_price": self.entry_price,
            "stop_loss":   self.stop_loss,
            "target":      self.target,
            "quantity":    self.quantity,
            "entry_time":  self.entry_time.isoformat(),
            "exit_price":  self.exit_price,
            "exit_time":   self.exit_time.isoformat() if self.exit_time else None,
            "pnl":         round(self.pnl, 2),
            "status":      self.status,
            "exit_reason": self.exit_reason,
        }


@dataclass
class RiskGuardVerdict:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
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
        self._kill_reason = ""
        self._paused_until: Optional[datetime] = None
        self._consecutive_losses = 0

        self._square_off   = time(*map(int, self.cfg["square_off_time"].split(":")))
        self._no_new_after = time(*map(int, self.cfg["no_new_trades_after"].split(":")))

        # Period risk tracking (updated from live P&L feed)
        self._week_pnl  : float = 0.0   # closed P&L for current ISO week before today
        self._month_pnl : float = 0.0   # closed P&L for current month before today
        self._size_mult : float = 1.0   # 0.5 when monthly drawdown 4–8%, 1.0 otherwise

    # ── State queries ──────────────────────────────────────────────────────

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

    # ── Risk gate ──────────────────────────────────────────────────────────

    def set_period_pnl(self, week_pnl: float, month_pnl: float) -> None:
        """
        Inject rolling P&L context from an external source (e.g. broker API).
        Call once at session start so weekly/monthly checks have full context.

        week_pnl  — net P&L for the current ISO week EXCLUDING today
        month_pnl — net P&L for the current calendar month EXCLUDING today
        """
        self._week_pnl  = week_pnl
        self._month_pnl = month_pnl

    def check_new_trade(self, signal: Signal, current_time: datetime
                        ) -> RiskGuardVerdict:
        if self._killed:
            return RiskGuardVerdict(False, "KILL SWITCH ACTIVE \u2014 session terminated")

        if current_time.time() >= self._no_new_after:
            return RiskGuardVerdict(
                False,
                f"No new trades after {self.cfg['no_new_trades_after']} "
                f"(current: {current_time.strftime('%H:%M')})",
            )

        if self.is_paused:
            remaining = int((self._paused_until - datetime.now()).total_seconds() // 60)
            return RiskGuardVerdict(
                False,
                f"PAUSED \u2014 {self._consecutive_losses} consecutive losses. "
                f"Resumes in {remaining} min",
            )

        if self.total_trades_today >= self.cfg["max_trades_per_day"]:
            return RiskGuardVerdict(
                False,
                f"Max trades reached ({self.cfg['max_trades_per_day']}/day)",
            )

        if len(self.open_positions) >= self.cfg["max_concurrent_positions"]:
            return RiskGuardVerdict(
                False,
                f"Max concurrent positions ({self.cfg['max_concurrent_positions']})",
            )

        max_loss = self.cfg["capital"] * (self.cfg["max_daily_loss_pct"] / 100)
        if self.realized_pnl <= -max_loss:
            self._kill("Daily loss limit hit")
            return RiskGuardVerdict(
                False,
                f"KILL: Daily loss \u20b9{abs(self.realized_pnl):,.0f} "
                f"exceeds limit \u20b9{max_loss:,.0f}",
            )

        # Weekly drawdown gate (hard block)
        capital = self.cfg["capital"]
        total_week  = self._week_pnl  + self.realized_pnl
        total_month = self._month_pnl + self.realized_pnl
        week_limit  = capital * (self.cfg.get("max_weekly_loss_pct",  3.0) / 100)
        month_limit = capital * (self.cfg.get("max_monthly_loss_pct", 8.0) / 100)
        if total_week <= -week_limit:
            return RiskGuardVerdict(
                False,
                f"WEEKLY RISK GATE: Week P&L \u20b9{total_week:,.0f} \u2264 "
                f"-\u20b9{week_limit:,.0f} ({self.cfg.get('max_weekly_loss_pct', 3.0):.0f}% capital)",
            )
        # Monthly drawdown: reduce size to 50% (not a hard block)
        if total_month <= -month_limit:
            self._size_mult = 0.5
        else:
            self._size_mult = 1.0

        max_profit = self.cfg["capital"] * (self.cfg["max_daily_profit_pct"] / 100)
        if self.realized_pnl >= max_profit:
            self._kill("Daily profit target reached \u2014 greed guard")
            return RiskGuardVerdict(
                False,
                f"GREED GUARD: Profit \u20b9{self.realized_pnl:,.0f} hit cap.",
            )

        risk_per_share = signal.risk_per_share
        if risk_per_share <= 0:
            return RiskGuardVerdict(False, "Risk per share is zero")

        max_qty = int(self.cfg["max_loss_per_trade"] / risk_per_share)
        if max_qty <= 0:
            return RiskGuardVerdict(
                False,
                f"Risk/share \u20b9{risk_per_share:.2f} exceeds max trade risk "
                f"\u20b9{self.cfg['max_loss_per_trade']}",
            )

        return RiskGuardVerdict(True)

    def calculate_position_size(self, signal: Signal) -> int:
        risk_per_share = signal.risk_per_share
        if risk_per_share <= 0:
            return 0

        qty = int(self.cfg["max_loss_per_trade"] / risk_per_share)

        # Cap by available capital
        capital_available = self.cfg["capital"] * self.cfg["leverage"]
        committed = sum(t.entry_price * t.quantity for t in self.open_positions)
        available = capital_available - committed
        qty_by_capital = int(available / signal.price) if signal.price > 0 else 0

        return int(min(qty, qty_by_capital) * self._size_mult)

    # ── Trade lifecycle ────────────────────────────────────────────────────

    def record_entry(self, signal: Signal, quantity: int) -> TradeRecord:
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
        with self._lock:
            for trade in reversed(self._trades):
                if trade.symbol == symbol and trade.is_open:
                    trade.close(exit_price, exit_time, reason)

                    if trade.status == "LOSS":
                        self._consecutive_losses += 1
                        if self._consecutive_losses >= self.cfg["consecutive_loss_pause"]:
                            self._pause()
                    else:
                        self._consecutive_losses = 0

                    # NOTE: _check_daily_limits MUST be called while _lock is held.
                    # It does NOT call realized_pnl to avoid re-entrant deadlock.
                    self._check_daily_limits_locked()
                    return trade
        return None

    def square_off_all(self, prices: dict[str, float],
                       current_time: datetime) -> list[TradeRecord]:
        closed = []
        for trade in self.open_positions:
            price = prices.get(trade.symbol, trade.entry_price)
            trade.close(price, current_time, "SQUAREOFF")
            closed.append(trade)
            self._consecutive_losses = 0
        return closed

    # ── Internal ──────────────────────────────────────────────────────────

    def _kill(self, reason: str) -> None:
        self._killed = True
        self._kill_reason = reason

    def _pause(self) -> None:
        duration = self.cfg["pause_duration_minutes"]
        self._paused_until = datetime.now() + timedelta(minutes=duration)

    def _check_daily_limits_locked(self) -> None:
        """
        Check daily loss limit. MUST only be called while self._lock is held.
        Sums PnL directly from _trades — does NOT call self.realized_pnl
        which would try to re-acquire _lock and deadlock.
        """
        max_loss = self.cfg["capital"] * (self.cfg["max_daily_loss_pct"] / 100)
        pnl = sum(t.pnl for t in self._trades if not t.is_open)
        if pnl <= -max_loss:
            self._kill(f"Daily loss limit: \u20b9{abs(pnl):,.0f}")

    def should_square_off(self, current_time: datetime) -> bool:
        return current_time.time() >= self._square_off

    def get_status(self) -> dict:
        return {
            "killed":             self._killed,
            "kill_reason":        self._kill_reason,
            "paused":             self.is_paused,
            "paused_until":       self._paused_until.isoformat() if self._paused_until else None,
            "consecutive_losses": self._consecutive_losses,
            "total_trades":       self.total_trades_today,
            "max_trades":         self.cfg["max_trades_per_day"],
            "open_positions":     len(self.open_positions),
            "max_positions":      self.cfg["max_concurrent_positions"],
            "realized_pnl":       round(self.realized_pnl, 2),
            "daily_pnl":          round(self.daily_pnl, 2),
            "max_daily_loss":     self.cfg["capital"] * (self.cfg["max_daily_loss_pct"] / 100),
            "max_daily_profit":   self.cfg["capital"] * (self.cfg["max_daily_profit_pct"] / 100),
        }

    def reset(self) -> None:
        with self._lock:
            self._trades.clear()
            self._killed = False
            self._kill_reason = ""
            self._paused_until = None
            self._consecutive_losses = 0
