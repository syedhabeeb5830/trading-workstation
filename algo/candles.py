"""
algo/candles.py — Real-time Candle Aggregator
===============================================
Builds OHLCV candles from raw tick data (KiteTicker) or 1-min bars.
Supports multiple timeframes simultaneously.
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
        if self.open == 0:
            return 0.0
        return ((self.high - self.low) / self.open) * 100

    def body_pct(self) -> float:
        if self.open == 0:
            return 0.0
        return abs(self.close - self.open) / self.open * 100

    def is_bullish(self) -> bool:
        return self.close > self.open

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "open": self.open, "high": self.high,
            "low": self.low, "close": self.close,
            "volume": self.volume,
        }


class CandleBuilder:
    """
    Aggregates ticks into candles for a single instrument.
    Fires on_candle_close callback when a candle completes.
    Thread-safe for use with KiteTicker.
    """

    def __init__(
        self,
        symbol: str,
        interval_minutes: int,
        on_candle_close: Optional[Callable[[str, int, Candle, list[Candle]], None]] = None,
        market_open: time = time(9, 15),
    ):
        self.symbol = symbol
        self.interval = interval_minutes
        self.on_candle_close = on_candle_close
        self.market_open = market_open

        self._current: Optional[Candle] = None
        self._candles: list[Candle] = []
        self._current_slot_end: Optional[datetime] = None
        self._lock = threading.Lock()

    @property
    def candles(self) -> list[Candle]:
        with self._lock:
            return list(self._candles)

    @property
    def current_candle(self) -> Optional[Candle]:
        with self._lock:
            return self._current

    def _slot_start(self, ts: datetime) -> datetime:
        market_start = ts.replace(
            hour=self.market_open.hour,
            minute=self.market_open.minute,
            second=0, microsecond=0,
        )
        elapsed = (ts - market_start).total_seconds()
        slot_idx = int(elapsed // (self.interval * 60))
        return market_start + timedelta(seconds=slot_idx * self.interval * 60)

    def _slot_end(self, slot_start: datetime) -> datetime:
        return slot_start + timedelta(minutes=self.interval)

    def on_tick(self, price: float, volume: int, tick_time: datetime) -> None:
        """Process a tick. Thread-safe."""
        with self._lock:
            if self._current is None:
                slot_start = self._slot_start(tick_time)
                self._current = Candle(
                    timestamp=slot_start,
                    open=price, high=price, low=price, close=price,
                    volume=volume,
                )
                self._current_slot_end = self._slot_end(slot_start)
                return

            if tick_time >= self._current_slot_end:
                completed = self._current
                self._candles.append(completed)
                slot_start = self._slot_start(tick_time)
                self._current = Candle(
                    timestamp=slot_start,
                    open=price, high=price, low=price, close=price,
                    volume=volume,
                )
                self._current_slot_end = self._slot_end(slot_start)

                if self.on_candle_close:
                    cb = self.on_candle_close
                    candles_copy = list(self._candles)
                    threading.Thread(
                        target=cb,
                        args=(self.symbol, self.interval, completed, candles_copy),
                        daemon=True,
                    ).start()
                return

            self._current.high = max(self._current.high, price)
            self._current.low = min(self._current.low, price)
            self._current.close = price
            self._current.volume += volume

    def get_current(self) -> Optional[Candle]:
        with self._lock:
            return self._current

    def reset(self) -> None:
        with self._lock:
            self._current = None
            self._candles.clear()
            self._current_slot_end = None


class MultiTimeframeCandleManager:
    """
    Manages candle builders for multiple instruments x multiple timeframes.
    Single entry point for all tick data.
    """

    def __init__(
        self,
        symbols: list[str],
        intervals: list[int],
        on_candle_close: Optional[Callable] = None,
    ):
        self._builders: dict[tuple[str, int], CandleBuilder] = {}
        for symbol in symbols:
            for interval in intervals:
                self._builders[(symbol, interval)] = CandleBuilder(
                    symbol=symbol,
                    interval_minutes=interval,
                    on_candle_close=on_candle_close,
                )

    def on_tick(self, symbol: str, price: float, volume: int,
                tick_time: datetime) -> None:
        """Route a tick to all builders for this symbol."""
        for (sym, _), builder in self._builders.items():
            if sym == symbol:
                builder.on_tick(price, volume, tick_time)

    def get_candles(self, symbol: str, interval: int) -> list[Candle]:
        return self._builders.get((symbol, interval), CandleBuilder(symbol, interval)).candles

    def get_current(self, symbol: str, interval: int) -> Optional[Candle]:
        b = self._builders.get((symbol, interval))
        return b.get_current() if b else None
