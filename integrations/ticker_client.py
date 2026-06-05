"""
integrations/ticker_client.py — KiteTicker WebSocket manager.

Provides automatic reconnection with exponential back-off and subscription
restore after reconnect.  Does NOT change the existing REST-based live_data
path — this module is opt-in infrastructure for any long-running process
(e.g. an algo engine) that needs a persistent WebSocket feed.

Reconnection policy:
  delay = min(base * multiplier^attempt, cap)  + ±25% jitter
  base=1s, multiplier=2, cap=60s

  attempt 0 →  1 s
  attempt 1 →  2 s
  attempt 2 →  4 s
  attempt 3 →  8 s
  attempt 4 → 16 s
  attempt 5 → 32 s
  attempt 6 → 60 s (capped)

Thread model:
  A single daemon thread runs the blocking ticker.connect() call.
  All public methods (subscribe, unsubscribe, stop) are thread-safe
  via a threading.Lock.  Callbacks fire on the ticker's internal thread.

Usage:
    from integrations.ticker_client import TickerManager

    mgr = TickerManager(api_key="…", access_token="…")
    mgr.on_tick(lambda ticks: print(ticks))
    mgr.subscribe([738561, 779521])   # instrument tokens
    mgr.start()
    …
    mgr.stop()
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable, Iterable, Set

logger = logging.getLogger(__name__)

# ── Reconnect policy ─────────────────────────────────────────────────────────
_BACKOFF_BASE: float = 1.0
_BACKOFF_CAP:  float = 60.0
_BACKOFF_MULT: float = 2.0
_JITTER_PCT:   float = 0.25   # ±25%


def _backoff(attempt: int) -> float:
    """Return seconds to wait before the *attempt*-th reconnect."""
    delay  = min(_BACKOFF_BASE * (_BACKOFF_MULT ** attempt), _BACKOFF_CAP)
    jitter = delay * _JITTER_PCT * (2 * random.random() - 1)
    return max(0.1, delay + jitter)


# ─────────────────────────────────────────────────────────────────────────────
class TickerManager:
    """
    KiteTicker wrapper with automatic reconnection.

    Subscriptions are stored in memory and re-applied every time the
    WebSocket reconnects, so a network blip never silently drops symbols.
    """

    def __init__(self, api_key: str, access_token: str) -> None:
        self._api_key      = api_key
        self._access_token = access_token

        # Subscription set — preserved across reconnects
        self._subscriptions: Set[int] = set()

        # Registered tick callbacks
        self._tick_cbs: list[Callable[[list], None]] = []
        self._connect_cbs: list[Callable[[], None]]  = []
        self._close_cbs:   list[Callable[[int, str], None]] = []
        self._error_cbs:   list[Callable[[int, str], None]] = []

        self._lock    = threading.Lock()
        self._running = False
        self._attempt = 0
        self._ticker  = None                    # KiteTicker instance
        self._thread: threading.Thread | None = None

    # ── Subscription management ───────────────────────────────────────────────

    def subscribe(self, tokens: Iterable[int]) -> None:
        """Add *tokens* to the subscription set and subscribe if connected."""
        tokens = list(tokens)
        with self._lock:
            self._subscriptions.update(tokens)
            t = self._ticker
        if t is not None:
            try:
                t.subscribe(tokens)
                t.set_mode(t.MODE_FULL, tokens)
            except Exception as exc:
                logger.warning("subscribe() failed (will restore on reconnect): %s", exc)

    def unsubscribe(self, tokens: Iterable[int]) -> None:
        """Remove *tokens* from the subscription set."""
        tokens = list(tokens)
        with self._lock:
            for tk in tokens:
                self._subscriptions.discard(tk)
            t = self._ticker
        if t is not None:
            try:
                t.unsubscribe(tokens)
            except Exception as exc:
                logger.warning("unsubscribe() error: %s", exc)

    # ── Callback registration ─────────────────────────────────────────────────

    def on_tick(self, cb: Callable[[list], None]) -> None:
        """Register a callback for incoming tick batches."""
        self._tick_cbs.append(cb)

    def on_connect(self, cb: Callable[[], None]) -> None:
        """Register a callback invoked after each successful (re)connect."""
        self._connect_cbs.append(cb)

    def on_close(self, cb: Callable[[int, str], None]) -> None:
        """Register a callback invoked when the connection closes."""
        self._close_cbs.append(cb)

    def on_error(self, cb: Callable[[int, str], None]) -> None:
        """Register a callback invoked on WebSocket errors."""
        self._error_cbs.append(cb)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """
        Start the managed WebSocket in a background daemon thread.
        Returns immediately; the connection happens asynchronously.
        """
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(
            target=self._run_loop, name="KiteTickerManager", daemon=True,
        )
        self._thread.start()
        logger.info("TickerManager started")

    def stop(self) -> None:
        """
        Signal the background thread to stop and close the WebSocket.
        Blocks for up to 3 seconds for a clean shutdown.
        """
        self._running = False
        with self._lock:
            t = self._ticker
        if t is not None:
            try:
                t.stop()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        logger.info("TickerManager stopped")

    # ── Internal loop ─────────────────────────────────────────────────────────

    def _build_ticker(self):
        """Construct a fresh KiteTicker with all callbacks wired."""
        try:
            from kiteconnect import KiteTicker  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "kiteconnect package not installed — run: pip install kiteconnect"
            ) from exc

        ticker              = KiteTicker(self._api_key, self._access_token)
        ticker.on_ticks     = self._handle_ticks
        ticker.on_connect   = self._handle_connect
        ticker.on_close     = self._handle_close
        ticker.on_error     = self._handle_error
        ticker.on_reconnect = self._handle_reconnect
        return ticker

    def _run_loop(self) -> None:
        """
        The main reconnect loop.  Runs on the daemon thread started by start().

        Each iteration:
          1. Build a fresh KiteTicker
          2. Call ticker.connect(threaded=False) — blocks until the socket closes
          3. If still _running, wait back-off seconds, then retry
        """
        while self._running:
            try:
                ticker = self._build_ticker()
                with self._lock:
                    self._ticker = ticker
                logger.info("Connecting to KiteTicker (attempt %d)…", self._attempt)
                ticker.connect(threaded=False)   # blocks until socket closes
            except Exception as exc:
                logger.error("KiteTicker connection error: %s", exc)
            finally:
                with self._lock:
                    self._ticker = None

            if not self._running:
                break

            delay = _backoff(self._attempt)
            logger.info("Reconnecting in %.1fs (attempt %d)…", delay, self._attempt)
            self._attempt += 1
            # Sleep in short slices so stop() can interrupt quickly
            deadline = time.monotonic() + delay
            while self._running and time.monotonic() < deadline:
                time.sleep(min(0.1, deadline - time.monotonic()))

    # ── Internal callbacks (called by KiteTicker's thread) ───────────────────

    def _handle_connect(self, ws, response) -> None:
        logger.info("KiteTicker connected")
        self._attempt = 0   # reset back-off on successful connect
        # Restore subscriptions
        with self._lock:
            tokens = list(self._subscriptions)
        if tokens:
            try:
                ws.subscribe(tokens)
                ws.set_mode(ws.MODE_FULL, tokens)
                logger.info("Subscriptions restored: %d token(s)", len(tokens))
            except Exception as exc:
                logger.warning("Subscription restore error: %s", exc)
        for cb in self._connect_cbs:
            _safe_call(cb)

    def _handle_close(self, ws, code, reason) -> None:
        logger.warning("KiteTicker closed — code=%s reason=%s", code, reason)
        for cb in self._close_cbs:
            _safe_call(cb, code, reason)

    def _handle_error(self, ws, code, reason) -> None:
        logger.error("KiteTicker error — code=%s reason=%s", code, reason)
        for cb in self._error_cbs:
            _safe_call(cb, code, reason)

    def _handle_reconnect(self, ws, attempts_count) -> None:
        logger.info("KiteTicker reconnecting (SDK attempt %d)", attempts_count)

    def _handle_ticks(self, ws, ticks) -> None:
        for cb in self._tick_cbs:
            _safe_call(cb, ticks)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_call(fn: Callable, *args) -> None:
    """Call *fn* with *args*, swallowing exceptions so one bad callback
    doesn't bring down the whole ticker thread."""
    try:
        fn(*args)
    except Exception as exc:
        logger.warning("Ticker callback %s raised: %s", getattr(fn, "__name__", fn), exc)