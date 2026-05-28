"""
algo/strategies.py — Pluggable Strategy Framework
===================================================
Each strategy implements the same interface:
  - evaluate(symbol, candle, candles, state) → Signal | None

Signal types: BUY, SELL, EXIT_LONG, EXIT_SHORT

Starting strategy: Opening Range Breakout (ORB)
  - Most proven intraday strategy globally
  - Simple rules, defined risk, time-boxed
  - Works on liquid large-caps with high win rate in trending markets
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
from typing import Optional

from algo.candles import Candle
from algo.config import ALGO_CONFIG


class SignalType(Enum):
    BUY = "BUY"
    SELL = "SELL"
    EXIT_LONG = "EXIT_LONG"
    EXIT_SHORT = "EXIT_SHORT"


@dataclass
class Signal:
    """Trading signal produced by a strategy."""
    type: SignalType
    symbol: str
    price: float
    stop_loss: float
    target: float
    timestamp: datetime
    strategy: str
    reason: str
    quantity: int = 0  # calculated by risk guard

    @property
    def risk_per_share(self) -> float:
        return abs(self.price - self.stop_loss)

    @property
    def reward_per_share(self) -> float:
        return abs(self.target - self.price)

    @property
    def rr_ratio(self) -> float:
        r = self.risk_per_share
        return self.reward_per_share / r if r > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "symbol": self.symbol,
            "price": self.price,
            "stop_loss": self.stop_loss,
            "target": self.target,
            "timestamp": self.timestamp.isoformat(),
            "strategy": self.strategy,
            "reason": self.reason,
            "quantity": self.quantity,
            "rr": round(self.rr_ratio, 2),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# ORB — Opening Range Breakout Strategy
# ═══════════════════════════════════════════════════════════════════════════════

class ORBState:
    """Per-symbol ORB state for the trading day."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.range_high: Optional[float] = None
        self.range_low: Optional[float] = None
        self.range_set: bool = False
        self.long_triggered: bool = False
        self.short_triggered: bool = False
        self.trade_taken: bool = False
        self.confirmation_count: int = 0
        self.last_signal_type: Optional[SignalType] = None

    @property
    def range_width(self) -> float:
        if self.range_high and self.range_low:
            return self.range_high - self.range_low
        return 0.0

    @property
    def range_pct(self) -> float:
        if self.range_low and self.range_low > 0:
            return (self.range_width / self.range_low) * 100
        return 0.0

    def is_valid_range(self) -> bool:
        """Check if opening range is tradeable."""
        cfg = ALGO_CONFIG
        pct = self.range_pct
        return cfg["orb_min_range_pct"] <= pct <= cfg["orb_max_range_pct"]


class ORBStrategy:
    """
    Opening Range Breakout — the bread-and-butter of institutional intraday.

    Rules:
    1. Collect the first N-minute candle as the "opening range"
    2. BUY when price breaks above range_high + buffer
    3. SELL when price breaks below range_low - buffer
    4. Stop loss: opposite end of the range
    5. Target: N× range width from entry
    6. One trade per stock per day (no re-entry after stop)
    7. No new trades after 14:30

    Edge: Opening range captures overnight order flow and institutional
    pre-market positioning. A breakout from this range with volume
    indicates directional commitment for the session.
    """

    NAME = "ORB"

    def __init__(self):
        self.states: dict[str, ORBState] = {}
        self._orb_timeframe = ALGO_CONFIG["orb_timeframe_minutes"]
        self._buffer_pct = ALGO_CONFIG["orb_buffer_pct"]
        self._target_mult = ALGO_CONFIG["orb_target_multiple"]
        self._max_trades = ALGO_CONFIG["orb_max_trades_per_stock"]
        self._confirm_needed = ALGO_CONFIG["orb_confirmation_candles"]
        self._no_trade_after = time(
            *map(int, ALGO_CONFIG["no_new_trades_after"].split(":"))
        )

    def _get_state(self, symbol: str) -> ORBState:
        if symbol not in self.states:
            self.states[symbol] = ORBState(symbol)
        return self.states[symbol]

    def set_opening_range(self, symbol: str, candle: Candle) -> None:
        """
        Called when the first candle (ORB timeframe) completes.
        Sets the opening range for the day.
        """
        state = self._get_state(symbol)
        state.range_high = candle.high
        state.range_low = candle.low
        state.range_set = True

    def evaluate(
        self,
        symbol: str,
        candle: Candle,
        candles: list[Candle],
        tick_time: datetime,
    ) -> Optional[Signal]:
        """
        Evaluate current candle against ORB levels.
        Returns a Signal if conditions are met, None otherwise.
        """
        state = self._get_state(symbol)

        # ── Phase 1: Set opening range from first candle ──────────────
        if not state.range_set:
            # The first completed candle IS the opening range
            if len(candles) >= 1:
                self.set_opening_range(symbol, candles[0])
            return None

        # ── Guard: range must be valid ────────────────────────────────
        if not state.is_valid_range():
            return None

        # ── Guard: already traded this stock today ────────────────────
        if state.trade_taken:
            return None

        # ── Guard: no new trades after cutoff ─────────────────────────
        if tick_time.time() >= self._no_trade_after:
            return None

        # ── Calculate levels ──────────────────────────────────────────
        buffer = state.range_high * (self._buffer_pct / 100)
        buy_trigger = state.range_high + buffer
        sell_trigger = state.range_low - buffer

        price = candle.close

        # ── LONG signal ───────────────────────────────────────────────
        if price > buy_trigger and not state.long_triggered:
            state.long_triggered = True
            state.trade_taken = True
            state.last_signal_type = SignalType.BUY

            stop = state.range_low
            target = price + (state.range_width * self._target_mult)

            return Signal(
                type=SignalType.BUY,
                symbol=symbol,
                price=price,
                stop_loss=stop,
                target=target,
                timestamp=tick_time,
                strategy=self.NAME,
                reason=f"ORB breakout above {state.range_high:.2f} "
                       f"(range: {state.range_pct:.2f}%, RR: "
                       f"{(target - price) / (price - stop):.1f}x)",
            )

        # ── SHORT signal ──────────────────────────────────────────────
        if price < sell_trigger and not state.short_triggered:
            state.short_triggered = True
            state.trade_taken = True
            state.last_signal_type = SignalType.SELL

            stop = state.range_high
            target = price - (state.range_width * self._target_mult)

            return Signal(
                type=SignalType.SELL,
                symbol=symbol,
                price=price,
                stop_loss=stop,
                target=target,
                timestamp=tick_time,
                strategy=self.NAME,
                reason=f"ORB breakdown below {state.range_low:.2f} "
                       f"(range: {state.range_pct:.2f}%, RR: "
                       f"{(price - target) / (stop - price):.1f}x)",
            )

        return None

    def check_exit(
        self,
        symbol: str,
        price: float,
        entry_price: float,
        stop_loss: float,
        target: float,
        is_long: bool,
        tick_time: datetime,
    ) -> Optional[Signal]:
        """Check if an open position should be exited."""
        if is_long:
            if price <= stop_loss:
                return Signal(
                    type=SignalType.EXIT_LONG,
                    symbol=symbol,
                    price=price,
                    stop_loss=stop_loss,
                    target=target,
                    timestamp=tick_time,
                    strategy=self.NAME,
                    reason=f"SL hit at {price:.2f} (stop was {stop_loss:.2f})",
                )
            if price >= target:
                return Signal(
                    type=SignalType.EXIT_LONG,
                    symbol=symbol,
                    price=price,
                    stop_loss=stop_loss,
                    target=target,
                    timestamp=tick_time,
                    strategy=self.NAME,
                    reason=f"Target hit at {price:.2f} (target was {target:.2f})",
                )
        else:
            if price >= stop_loss:
                return Signal(
                    type=SignalType.EXIT_SHORT,
                    symbol=symbol,
                    price=price,
                    stop_loss=stop_loss,
                    target=target,
                    timestamp=tick_time,
                    strategy=self.NAME,
                    reason=f"SL hit at {price:.2f} (stop was {stop_loss:.2f})",
                )
            if price <= target:
                return Signal(
                    type=SignalType.EXIT_SHORT,
                    symbol=symbol,
                    price=price,
                    stop_loss=stop_loss,
                    target=target,
                    timestamp=tick_time,
                    strategy=self.NAME,
                    reason=f"Target hit at {price:.2f} (target was {target:.2f})",
                )

        return None

    def reset(self) -> None:
        """Reset all state for a new trading day."""
        self.states.clear()

    def get_status(self) -> dict:
        """Get current ORB state for all symbols."""
        status = {}
        for sym, state in self.states.items():
            status[sym] = {
                "range_high": state.range_high,
                "range_low": state.range_low,
                "range_pct": round(state.range_pct, 2),
                "range_valid": state.is_valid_range(),
                "long_triggered": state.long_triggered,
                "short_triggered": state.short_triggered,
                "trade_taken": state.trade_taken,
            }
        return status


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY REGISTRY
# ═══════════════════════════════════════════════════════════════════════════════

STRATEGIES = {
    "ORB": ORBStrategy,
}


def get_strategy(name: str = "ORB"):
    """Factory function to get a strategy instance by name."""
    cls = STRATEGIES.get(name.upper())
    if cls is None:
        raise ValueError(f"Unknown strategy: {name}. Available: {list(STRATEGIES.keys())}")
    return cls()
