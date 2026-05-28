"""
algo/candles.py — Real-time Candle Aggregator
===============================================
Builds OHLCV candles from raw tick data (KiteTicker) or 1-min bars.
Supports multiple timeframes simultaneously (5-min, 15-min).

Design:
  - Each instrument gets its own CandleBuilder
  - On each tick: updates the current candle
  - When a candle closes: fires a callback (strategy evaluation)
  - Thread-safe for use with KiteTicker's on_ticks callback
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Callable, Optional
import threading


@dataclass
class Candle:
    """Single OHLCV candle."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    def range_pct(self) -> float:
        """Range as percentage of open."""
        if self.open == 0:
            return 0.0
        return ((self.high - self.low) / self.open) * 100

    def body_pct(self) -> float:
        """Body size as percentage of open."""
        if self.open == 0:
            return 0.0
        return abs(self.close - self.open) / self.open * 100

    def is_bullish(self) -> bool:
        return self.close > self.open

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


class CandleBuilder:
    """
    Aggregates ticks into candles for a single instrument.
    Fires on_candle_close callback when a candle completes.
    """

    def __init__(
        self,
        symbol: str,
        interval_minutes: int,
        on_candle_close: Optional[Callable[[str, int, Candle, list[Candle]], None]] = None,
        market_open: time = time(9, 15),
        market_close: time = time(15, 30),
    ):
        self.symbol = symbol
        self.interval = interval_minutes
        self.on_candle_close = on_candle_close
        self.market_open = market_open
        self.market_close = market_close

        self._current: Optional[Candle] = None
        self._candles: list[Candle] = []
        self._current_slot_end: Optional[datetime] = None
        self._lock = threading.Lock()

    @property
    def candles(self) -> list[Candle]:
        """All completed candles for today."""
        with self._lock:
            return list(self._candles)

    @property
    def current_candle(self) -> Optional[Candle]:
        with self._lock:
            return self._current

    def _slot_start(self, ts: datetime) -> datetime:
        """Calculate the start of the candle slot containing this timestamp."""
        market_start = ts.replace(
            hour=self.market_open.hour,
            minute=self.market_open.minute,
            second=0, microsecond=0
        )
        elapsed = (ts - market_start).total_seconds()
        slot_seconds = self.interval * 60
        slot_index = int(elapsed // slot_seconds)
        return market_start + timedelta(seconds=slot_index * slot_seconds)

    def _slot_end(self, slot_start: datetime) -> datetime:
        return slot_start + timedelta(minutes=self.interval)

    def on_tick(self, price: float, volume: int, tick_time: datetime) -> None:
        """
        Process a single tick. Called from KiteTicker on_ticks callback.
        Thread-safe.
        """
        with self._lock:
            # First tick of the day
            if self._current is None:
                slot_start = self._slot_start(tick_time)
                self._current = Candle(
                    timestamp=slot_start,
                    open=price, high=price, low=price, close=price,
                    volume=volume,
                )
                self._current_slot_end = self._slot_end(slot_start)
                return

            # Check if we've crossed into a new candle slot
            if tick_time >= self._current_slot_end:
                # Close current candle
                completed = self._current
                self._candles.append(completed)

                # Start new candle
                slot_start = self._slot_start(tick_time)
                self._current = Candle(
                    timestamp=slot_start,
                    open=price, high=price, low=price, close=price,
                    volume=volume,
                )
                self._current_slot_end = self._slot_end(slot_start)

                # Fire callback (outside lock to prevent deadlocks)
                if self.on_candle_close:
                    # Release lock before callback
                    cb = self.on_candle_close
                    candles_copy = list(self._candles)
                    # We need to call outside the lock
                    threading.Thread(
                        target=cb,
                        args=(self.symbol, self.interval, completed, candles_copy),
                        daemon=True,
                    ).start()
                return

            # Update current candle
            self._current.high = max(self._current.high, price)
            self._current.low = min(self._current.low, price)
            self._current.close = price
            self._current.volume += volume

    def force_close(self) -> Optional[Candle]:
        """Force-close the current candle (used at EOD square-off)."""
        with self._lock:
            if self._current is not None:
                self._candles.append(self._current)
                closed = self._current
                self._current = None
                return closed
            return None

    def reset(self) -> None:
        """Reset for a new trading day."""
        with self._lock:
            self._current = None
            self._candles.clear()
            self._current_slot_end = None

    def feed_historical(self, bars: list[dict]) -> None:
        """
        Feed historical 1-min bars for simulation mode.
        Each bar: {"timestamp": datetime, "open":, "high":, "low":, "close":, "volume":}
        """
        for bar in bars:
            ts = bar["timestamp"]
            # Simulate 4 ticks per bar: open, high, low, close
            self.on_tick(bar["open"], bar["volume"] // 4, ts)
            self.on_tick(bar["high"], 0, ts + timedelta(seconds=10))
            self.on_tick(bar["low"], 0, ts + timedelta(seconds=20))
            self.on_tick(bar["close"], bar["volume"] - (bar["volume"] // 4),
                        ts + timedelta(seconds=50))


class MultiTimeframeCandleManager:
    """
    Manages candle builders for multiple instruments × multiple timeframes.
    Single entry point for all tick data.
    """

    def __init__(
        self,
        symbols: list[str],
        intervals: list[int],
        on_candle_close: Optional[Callable] = None,
    ):
        self.builders: dict[tuple[str, int], CandleBuilder] = {}
        for sym in symbols:
            for interval in intervals:
                key = (sym, interval)
                self.builders[key] = CandleBuilder(
                    symbol=sym,
                    interval_minutes=interval,
                    on_candle_close=on_candle_close,
                )

    def on_tick(self, symbol: str, price: float, volume: int, tick_time: datetime) -> None:
        """Route a tick to all timeframe builders for this symbol."""
        for (sym, _interval), builder in self.builders.items():
            if sym == symbol:
                builder.on_tick(price, volume, tick_time)

    def get_candles(self, symbol: str, interval: int) -> list[Candle]:
        """Get completed candles for a symbol/interval pair."""
        key = (symbol, interval)
        if key in self.builders:
            return self.builders[key].candles
        return []

    def get_current(self, symbol: str, interval: int) -> Optional[Candle]:
        key = (symbol, interval)
        if key in self.builders:
            return self.builders[key].current_candle
        return None

    def reset_all(self) -> None:
        for builder in self.builders.values():
            builder.reset()
