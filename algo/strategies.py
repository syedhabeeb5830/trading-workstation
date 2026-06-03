"""
algo/strategies.py — Trading Strategy Definitions
===================================================
Implements the ORB (Opening Range Breakout) strategy.

ORB logic:
  1. Record the High/Low of the first 15-min candle (09:15–09:30)
  2. On subsequent candles:
     - If price breaks ABOVE range_high + buffer → BUY signal
     - If price breaks BELOW range_low  - buffer → SHORT signal
  3. Stop = opposite end of opening range
  4. Target = entry ± (range_width × target_multiple)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Optional

from algo.candles import Candle
from algo.config import ALGO_CONFIG


class SignalType(Enum):
    BUY        = "BUY"
    SELL       = "SELL"
    EXIT_LONG  = "EXIT_LONG"
    EXIT_SHORT = "EXIT_SHORT"


@dataclass
class Signal:
    """A trading signal produced by a strategy."""
    type: SignalType
    symbol: str
    price: float
    stop_loss: float
    target: float
    timestamp: datetime
    strategy: str
    reason: str
    quantity: int = 0
    regime: str = "UNKNOWN"   # MarketRegime.value — set by engine after RegimeClassifier

    @property
    def risk_per_share(self) -> float:
        return abs(self.price - self.stop_loss)

    @property
    def rr_ratio(self) -> float:
        risk = self.risk_per_share
        if risk == 0:
            return 0.0
        return abs(self.target - self.price) / risk


@dataclass
class ORBState:
    """Per-symbol ORB state for the current session."""
    range_high: float = 0.0
    range_low: float = 0.0
    range_pct: float = 0.0
    range_set: bool = False
    trade_taken: bool = False
    long_triggered: bool = False
    short_triggered: bool = False
    last_signal_type: Optional[SignalType] = None

    @property
    def range_width(self) -> float:
        return self.range_high - self.range_low

    def is_valid_range(self) -> bool:
        return (ALGO_CONFIG["orb_min_range_pct"]
                <= self.range_pct
                <= ALGO_CONFIG["orb_max_range_pct"])


class ORBStrategy:
    """Opening Range Breakout strategy."""
    NAME = "ORB"

    _MIN_RR = 1.5    # Minimum reward:risk ratio — below this the trade isn't worth the commission + slippage

    def __init__(self):
        self._states: dict[str, ORBState] = {}
        self._buffer_pct   = ALGO_CONFIG["orb_buffer_pct"]
        self._target_mult  = ALGO_CONFIG["orb_target_multiple"]
        self._no_trade_after = time(*map(int, ALGO_CONFIG["no_new_trades_after"].split(":")))

    def _get_state(self, symbol: str) -> ORBState:
        if symbol not in self._states:
            self._states[symbol] = ORBState()
        return self._states[symbol]

    def set_opening_range(self, symbol: str, candle: Candle) -> None:
        """Set the opening range from the first completed 15-min candle."""
        state = self._get_state(symbol)
        state.range_high = candle.high
        state.range_low  = candle.low
        state.range_pct  = candle.range_pct()
        state.range_set  = True

    def evaluate(
        self,
        symbol: str,
        candle: Candle,
        candles: list[Candle],
        tick_time: datetime,
    ) -> Optional[Signal]:
        """
        Evaluate current candle against ORB levels.
        Returns Signal if breakout conditions are met.
        """
        state = self._get_state(symbol)

        if not state.range_set:
            if len(candles) >= 1:
                self.set_opening_range(symbol, candles[0])
            return None

        if not state.is_valid_range():
            return None

        if state.trade_taken:
            return None

        if tick_time.time() >= self._no_trade_after:
            return None

        buffer      = state.range_high * (self._buffer_pct / 100)
        buy_trigger = state.range_high + buffer
        sell_trigger = state.range_low - buffer
        price = candle.close

        # LONG breakout
        if price > buy_trigger and not state.long_triggered:
            state.long_triggered = True

            stop   = state.range_low
            target = price + (state.range_width * self._target_mult)
            rr     = (target - price) / (price - stop) if price != stop else 0

            # Institutional RR gate: reject setups with poor risk/reward
            if rr < self._MIN_RR:
                return None

            state.trade_taken      = True
            state.last_signal_type = SignalType.BUY

            return Signal(
                type=SignalType.BUY, symbol=symbol, price=price,
                stop_loss=stop, target=target, timestamp=tick_time,
                strategy=self.NAME,
                reason=(f"ORB breakout above {state.range_high:.2f} "
                        f"(range: {state.range_pct:.2f}%, RR: {rr:.1f}x)"),
            )

        # SHORT breakdown
        if price < sell_trigger and not state.short_triggered:
            state.short_triggered = True

            stop   = state.range_high
            target = price - (state.range_width * self._target_mult)
            rr     = (price - target) / (stop - price) if stop != price else 0

            # Institutional RR gate: reject setups with poor risk/reward
            if rr < self._MIN_RR:
                return None

            state.trade_taken      = True
            state.last_signal_type = SignalType.SELL

            return Signal(
                type=SignalType.SELL, symbol=symbol, price=price,
                stop_loss=stop, target=target, timestamp=tick_time,
                strategy=self.NAME,
                reason=(f"ORB breakdown below {state.range_low:.2f} "
                        f"(range: {state.range_pct:.2f}%, RR: {rr:.1f}x)"),
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
        """Check if an open position should be exited (SL or target)."""
        if is_long:
            if price <= stop_loss:
                return Signal(
                    type=SignalType.EXIT_LONG, symbol=symbol, price=price,
                    stop_loss=stop_loss, target=target, timestamp=tick_time,
                    strategy=self.NAME,
                    reason=f"SL hit at {price:.2f} (stop was {stop_loss:.2f})",
                )
            if price >= target:
                return Signal(
                    type=SignalType.EXIT_LONG, symbol=symbol, price=price,
                    stop_loss=stop_loss, target=target, timestamp=tick_time,
                    strategy=self.NAME,
                    reason=f"Target hit at {price:.2f} (target was {target:.2f})",
                )
        else:
            if price >= stop_loss:
                return Signal(
                    type=SignalType.EXIT_SHORT, symbol=symbol, price=price,
                    stop_loss=stop_loss, target=target, timestamp=tick_time,
                    strategy=self.NAME,
                    reason=f"SL hit at {price:.2f} (stop was {stop_loss:.2f})",
                )
            if price <= target:
                return Signal(
                    type=SignalType.EXIT_SHORT, symbol=symbol, price=price,
                    stop_loss=stop_loss, target=target, timestamp=tick_time,
                    strategy=self.NAME,
                    reason=f"Target hit at {price:.2f} (target was {target:.2f})",
                )
        return None

    def get_status(self) -> dict:
        return {
            sym: {
                "range_high":  s.range_high,
                "range_low":   s.range_low,
                "range_pct":   s.range_pct,
                "range_valid": s.is_valid_range(),
                "range_set":   s.range_set,
                "trade_taken": s.trade_taken,
            }
            for sym, s in self._states.items()
        }

    def reset(self) -> None:
        self._states.clear()


# ── Strategy registry ─────────────────────────────────────────────────────────
STRATEGIES = {"ORB": ORBStrategy}

def get_strategy(name: str = "ORB"):
    cls = STRATEGIES.get(name.upper())
    if cls is None:
        raise ValueError(f"Unknown strategy: {name}. Available: {list(STRATEGIES)}")
    return cls()
