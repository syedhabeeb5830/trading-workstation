"""
algo/engine.py — Algo Trading Engine (Main Orchestrator)
==========================================================
Three execution modes:
  1. LIVE     — KiteTicker WebSocket -> real-time strategy -> live orders
  2. PAPER    — Same flow, executor logs instead of placing orders
  3. SIMULATE — Replay yesterday's 1-min data through strategy (instant)

Usage via run.py:
  python run.py --algo              # paper mode (safe default)
  python run.py --algo --live       # real orders
  python run.py --algo --simulate   # replay yesterday
  python run.py --algo --status     # session P&L
"""

from __future__ import annotations
import signal as _signal
import sys
import time as _time_module
from datetime import datetime, time, date, timedelta
from typing import Optional

from algo.config import ALGO_CONFIG
from algo.candles import MultiTimeframeCandleManager, Candle
from algo.strategies import get_strategy, Signal, SignalType
from algo.risk_guard import RiskGuard, TradeRecord
from algo.executor import AlgoExecutor


# ANSI colours
_G, _Y, _R, _C, _B, _D, _RST = (
    "\033[92m", "\033[93m", "\033[91m",
    "\033[96m", "\033[1m", "\033[2m", "\033[0m",
)

# ── Sector map (NSE NIFTY 50) ──────────────────────────────────────────────
# Used by the sector-isolation gate: at most max_sector_positions open trades
# in the same industry group at any time.  Un-mapped symbols are skip-checked.
_SECTOR_MAP: dict[str, str] = {
    "INFY": "TECH",       "TCS": "TECH",        "HCLTECH": "TECH",
    "WIPRO": "TECH",      "TECHM": "TECH",
    "HDFCBANK": "BANK",   "ICICIBANK": "BANK",  "KOTAKBANK": "BANK",
    "AXISBANK": "BANK",   "SBIN": "BANK",
    "HINDUNILVR": "FMCG","ITC": "FMCG",        "NESTLEIND": "FMCG",
    "RELIANCE": "ENERGY", "BHARTIARTL": "TELECOM",
    "MARUTI": "AUTO",     "TATAMOTORS": "AUTO",  "EICHERMOT": "AUTO",
    "BAJFINANCE": "FINANCE",
    "SUNPHARMA": "PHARMA","DRREDDY": "PHARMA",
    "LT": "INFRA",        "ADANIPORTS": "INFRA",
    "TITAN": "CONSUMER",
    "ULTRACEMCO": "CEMENT","GRASIM": "CEMENT",
    "HDFCLIFE": "INSURANCE",
}


class AlgoEngine:
    """Core algo engine — orchestrates candle building, strategy, risk, execution."""

    def __init__(self, strategy_name: str = "ORB", paper_mode: bool = True,
                 journal_dir: str = "journal",
                 symbols: list[str] | None = None):
        self.paper_mode    = paper_mode
        self.journal_dir   = journal_dir
        self.strategy      = get_strategy(strategy_name)
        self.risk_guard    = RiskGuard(ALGO_CONFIG)
        self.executor      = AlgoExecutor(self.risk_guard, paper_mode, journal_dir)
        self.symbols       = symbols if symbols is not None else list(ALGO_CONFIG["instruments"])
        self.candle_manager = MultiTimeframeCandleManager(
            symbols=self.symbols,
            intervals=ALGO_CONFIG["candle_intervals"],
            on_candle_close=self._on_candle_close,
        )
        self._running        = False
        self._orb_tf         = ALGO_CONFIG["orb_timeframe_minutes"]
        self._square_off_time = time(*map(int, ALGO_CONFIG["square_off_time"].split(":")))
        self._token_to_symbol: dict[int, str] = {}

    # ══════════════════════════════════════════════════════════════════════
    # MODE 1: LIVE / PAPER  (KiteTicker WebSocket)
    # ══════════════════════════════════════════════════════════════════════

    def run_live(self) -> None:
        """Start the live/paper algo engine. Blocks until market close."""
        self._print_banner()
        self._running = True
        _signal.signal(_signal.SIGINT, self._handle_shutdown)

        try:
            from algo.instruments import resolve_tokens
            from integrations.zerodha import get_kite
            from kiteconnect import KiteTicker

            kite      = get_kite()
            token_map = resolve_tokens(self.symbols)

            if not token_map:
                print(f"\n  {_R}\u2717 Could not resolve instrument tokens.{_RST}")
                print(f"  {_D}  Ensure Kite session is active (--kite-login){_RST}\n")
                return

            self._token_to_symbol = {v: k for k, v in token_map.items()}
            tokens = list(token_map.values())

            print(f"  Subscribing to {len(tokens)} instruments...")
            for sym, tok in token_map.items():
                print(f"    {sym}: {tok}")

            kws = KiteTicker(kite.api_key, kite.access_token)
            kws.on_ticks   = self._on_ticks
            kws.on_connect = lambda ws, r: (
                ws.subscribe(tokens),
                ws.set_mode(ws.MODE_FULL, tokens),
            )
            kws.on_close = self._on_ws_close
            kws.on_error = self._on_ws_error

            mode_str = f"{_Y}PAPER MODE{_RST}" if self.paper_mode else f"{_R}LIVE MODE{_RST}"
            print(f"\n  {_G}\u25cf ENGINE RUNNING{_RST}  {mode_str}\n")

            kws.connect(threaded=True)

            while self._running:
                now = datetime.now()
                if now.time() >= self._square_off_time or self.risk_guard.is_killed:
                    self._square_off_all()
                    break
                self._check_exits(now)
                _time_module.sleep(1)

            kws.close()

        except ImportError as e:
            print(f"\n  {_R}\u2717 Missing dependency: {e}{_RST}")
            print(f"  {_D}  pip install kiteconnect{_RST}\n")
        except Exception as e:
            print(f"\n  {_R}\u2717 Engine error: {e}{_RST}\n")
        finally:
            self._print_session_summary()

    def _on_ticks(self, ws, ticks: list) -> None:
        for tick in ticks:
            token  = tick.get("instrument_token")
            symbol = self._token_to_symbol.get(token)
            if not symbol:
                continue
            price    = float(tick.get("last_price", 0))
            volume   = int(tick.get("volume_traded", 0))
            tick_ts  = tick.get("exchange_timestamp", datetime.now())
            if isinstance(tick_ts, str):
                tick_ts = datetime.fromisoformat(tick_ts)
            self.candle_manager.on_tick(symbol, price, volume, tick_ts)
            self._check_tick_exit(symbol, price, tick_ts)

    def _on_candle_close(self, symbol: str, interval: int,
                         candle: Candle, candles: list[Candle]) -> None:
        if interval != self._orb_tf:
            return
        now = candle.timestamp + timedelta(minutes=interval)
        sig = self.strategy.evaluate(symbol, candle, candles, now)
        if sig:
            self.executor.execute_signal(sig)

    def _check_tick_exit(self, symbol: str, price: float, ts: datetime) -> None:
        for trade in self.risk_guard.open_positions:
            if trade.symbol != symbol:
                continue
            exit_sig = self.strategy.check_exit(
                symbol, price, trade.entry_price,
                trade.stop_loss, trade.target, trade.is_long, ts,
            )
            if exit_sig:
                self.executor.execute_exit(trade, price, ts, exit_sig.reason)

    def _check_exits(self, now: datetime) -> None:
        pass  # tick-level exit checks cover this in live mode

    def _on_ws_close(self, ws, code, reason) -> None:
        if self._running:
            print(f"\n  {_Y}\u26a0 WebSocket closed: {reason}. Reconnecting...{_RST}")

    def _on_ws_error(self, ws, code, reason) -> None:
        print(f"  {_R}WebSocket error: {code} \u2014 {reason}{_RST}")

    def _handle_shutdown(self, signum, frame) -> None:
        print(f"\n\n  {_Y}\u26a0 SHUTDOWN \u2014 squaring off all positions...{_RST}")
        self._square_off_all()
        self._running = False

    def _square_off_all(self) -> None:
        positions = self.risk_guard.open_positions
        if not positions:
            return
        prices = {}
        for t in positions:
            c = self.candle_manager.get_current(t.symbol, self._orb_tf)
            prices[t.symbol] = c.close if c else t.entry_price
        closed = self.executor.execute_square_off(prices, datetime.now())
        if closed:
            print(f"\n  {_B}Squared off {len(closed)} position(s){_RST}")

    # ══════════════════════════════════════════════════════════════════════
    # MODE 3: SIMULATE  (Replay historical 1-min data)
    # ══════════════════════════════════════════════════════════════════════

    def run_simulate(self, sim_date: Optional[date] = None) -> None:
        """
        Replay the most recent completed trading session bar-by-bar through
        the ORB strategy.  Uses yfinance 1-min data; falls back to Kite
        historical API if yfinance has no data.

        Off-market note
        ---------------
        Yahoo Finance's 1-min NSE feed is only reliably available for the
        **current open session** and occasionally the most recent completed
        session.  Running --simulate outside market hours (09:15–15:30 IST)
        will automatically scan back up to 5 trading days to find a session
        with available 1-min data.  Use ``--backtest`` for reproducible
        historical analysis — it uses 15-min data which is stable up to 60
        calendar days.
        """
        self.executor._sim_mode = True

        now = datetime.now()
        market_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
        if not (market_open <= now <= market_close):
            print(f"\n  {_Y}  Off-market hours ({now.strftime('%H:%M')} IST)."
                  f"  Scanning back for most recent session with 1-min data…{_RST}")

        target_date = sim_date or self._get_last_trading_day()
        self._print_sim_banner(target_date)

        # Download NIFTY 50 1-min bars once — used as intraday direction bias.
        # If NIFTY is net-positive at entry time we block SHORTs; net-negative blocks LONGs.
        # Threshold: ±0.2% from NIFTY open — within that band both directions are allowed.
        print(f"\n  {_D}  Downloading NIFTY 50 1-min data for direction bias...{_RST}", flush=True)
        nifty_bars = self._fetch_nifty_bars_1m(target_date)
        if nifty_bars:
            nifty_open = nifty_bars[0]["close"]
            nifty_map  = {b["timestamp"]: b["close"] for b in nifty_bars}
            print(f"  {_D}  NIFTY open: {nifty_open:,.2f}  "
                  f"({len(nifty_bars)} bars){_RST}")
        else:
            nifty_open = None
            nifty_map  = {}
            print(f"  {_Y}  NIFTY 1-min data unavailable — direction bias disabled{_RST}")

        # ── Regime classification — skip CHOP sessions entirely ───────────────
        # Build 15-min NIFTY candles from the 1-min bars already downloaded,
        # then run RegimeClassifier on the first 5 (09:15–10:15).
        # CHOP = coin-flip breakouts; no ORB edge, skip all trades today.
        if nifty_bars:
            from algo.regime import RegimeClassifier, MarketRegime
            _nifty_15m   = self._agg_1m_to_15m(nifty_bars)
            _regime_rslt = RegimeClassifier().classify(_nifty_15m)
            regime_str   = _regime_rslt.regime.value
            _rclr = (_R if regime_str == "CHOP"
                     else _G if "TREND" in regime_str else _Y)
            print(f"  {_rclr}  Regime: {regime_str}  "
                  f"({_regime_rslt.reason}){_RST}")
            if _regime_rslt.regime == MarketRegime.CHOP:
                print(f"\n  {_R}  CHOP day — ORB has no edge in sideways markets.{_RST}")
                print(f"  {_D}  All {len(self.symbols)} symbols skipped. "
                      f"Capital preserved.{_RST}\n")
                self._print_sim_summary(
                    target_date,
                    {s: {"valid": False, "traded": False} for s in self.symbols},
                )
                return
        else:
            print(f"  {_Y}  Regime: UNKNOWN (no NIFTY data){_RST}")

        results: dict[str, dict] = {}

        for symbol in self.symbols:
            print(f"\n  {_B}{'─' * 56}{_RST}")
            print(f"  {_C}{symbol}{_RST}  — downloading 1-min data...", flush=True)

            bars = self._fetch_bars(symbol, target_date)

            if not bars:
                print(f"  {_Y}  No 1-min data found in the last 5 trading days.{_RST}")
                print(f"  {_D}  (Yahoo Finance 1-min NSE data is only available"
                      f" during/right after market hours.){_RST}")
                results[symbol] = {"valid": False, "traded": False}
                continue

            actual_date = bars[0]["timestamp"].date()
            print(f"  {_D}  {len(bars)} bars loaded  "
                  f"({bars[0]['timestamp'].strftime('%H:%M')} → "
                  f"{bars[-1]['timestamp'].strftime('%H:%M')}"
                  f"  date: {actual_date}){_RST}", flush=True)

            self._replay_symbol(symbol, bars, actual_date, results,
                                nifty_map=nifty_map, nifty_open=nifty_open)

        self._print_sim_summary(target_date, results)

    def _fetch_bars(self, symbol: str, target_date: date) -> list[dict]:
        """Download 1-min bars for *target_date*, trying yfinance first then Kite.

        Yahoo Finance 1-min data for NSE stocks is only reliably available for
        the **current open session** or the **most recently completed session**.
        Off-market and overnight requests often fail even for valid trading days
        because Yahoo hasn't yet indexed the previous session's 1-min feed.

        Strategy: try target_date, then walk back up to 4 more trading days
        until data is found.  This makes --simulate work reliably when run
        any time of day without manual date selection.
        """
        candidate = target_date
        for _ in range(5):
            bars = self._fetch_yfinance(symbol, candidate)
            if bars:
                if candidate != target_date:
                    print(f"  {_Y}  (using {candidate} — {target_date} had no 1-min data){_RST}")
                return bars
            # Step back one trading day
            candidate -= timedelta(days=1)
            while candidate.weekday() >= 5:          # skip weekends
                candidate -= timedelta(days=1)

        # yfinance exhausted — try Kite for the original target date
        try:
            bars = self._fetch_kite(symbol, target_date)
            if bars:
                return bars
        except Exception:
            pass
        return []

    def _fetch_yfinance(self, symbol: str, target_date: date) -> list[dict]:
        """Fetch 1-min bars from Yahoo Finance for *symbol* on *target_date*.

        Yahoo Finance 1-min NSE data is unreliable off-market — it silently
        returns an empty result (with a misleading "possibly delisted" warning
        printed to stderr).  All yfinance output is suppressed here; the caller
        checks for empty return and retries on the previous trading day.
        """
        import contextlib, io
        import yfinance as yf
        import pandas as pd

        ticker = f"{symbol}.NS"
        _sink  = io.StringIO()
        with contextlib.redirect_stdout(_sink), contextlib.redirect_stderr(_sink):
            df = yf.download(
                ticker,
                start=str(target_date),
                end=str(target_date + timedelta(days=1)),
                interval="1m",
                auto_adjust=True,
                progress=False,
            )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty:
            return []

        bars = []
        for idx, row in df.iterrows():
            ts = idx.to_pydatetime()
            if hasattr(ts, "tzinfo") and ts.tzinfo:
                ts = ts.replace(tzinfo=None)
            # Filter to NSE session hours only
            if not (time(9, 15) <= ts.time() <= time(15, 30)):
                continue
            bars.append({
                "timestamp": ts,
                "open":   float(row["Open"]),
                "high":   float(row["High"]),
                "low":    float(row["Low"]),
                "close":  float(row["Close"]),
                "volume": int(row.get("Volume", 0)),
            })
        return bars

    def _fetch_nifty_bars_1m(self, target_date: date) -> list[dict]:
        """Download 1-min NIFTY 50 bars for the direction-bias filter.

        Uses ``^NSEI`` ticker (no .NS suffix — NIFTY is an index, not a stock).
        Same fallback/suppression pattern as ``_fetch_yfinance``.
        """
        import contextlib, io
        import yfinance as yf
        import pandas as pd

        _sink     = io.StringIO()
        candidate = target_date
        for _ in range(5):
            with contextlib.redirect_stdout(_sink), contextlib.redirect_stderr(_sink):
                df = yf.download(
                    "^NSEI",
                    start=str(candidate),
                    end=str(candidate + timedelta(days=1)),
                    interval="1m",
                    auto_adjust=True,
                    progress=False,
                )
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            if not df.empty:
                bars = []
                for idx, row in df.iterrows():
                    ts = idx.to_pydatetime()
                    if hasattr(ts, "tzinfo") and ts.tzinfo:
                        ts = ts.replace(tzinfo=None)
                    if not (time(9, 15) <= ts.time() <= time(15, 30)):
                        continue
                    bars.append({
                        "timestamp": ts,
                        "open":   float(row.get("Open",  row["Close"])),
                        "high":   float(row.get("High",  row["Close"])),
                        "low":    float(row.get("Low",   row["Close"])),
                        "close":  float(row["Close"]),
                        "volume": int(row.get("Volume", 0)),
                    })
                if bars:
                    return bars
            candidate -= timedelta(days=1)
            while candidate.weekday() >= 5:
                candidate -= timedelta(days=1)
        return []

    def _fetch_kite(self, symbol: str, target_date: date) -> list[dict]:
        from integrations.zerodha import get_kite
        from algo.instruments import resolve_tokens

        kite      = get_kite()
        token_map = resolve_tokens([symbol])
        if symbol not in token_map:
            return []

        token = token_map[symbol]
        raw   = kite.historical_data(
            instrument_token=token,
            from_date=datetime.combine(target_date, time(9, 15)),
            to_date=datetime.combine(target_date, time(15, 30)),
            interval="minute",
        )
        if not raw:
            return []

        bars = []
        for r in raw:
            bars.append({
                "timestamp": r["date"].replace(tzinfo=None) if hasattr(r["date"], "tzinfo") else r["date"],
                "open":   float(r["open"]),
                "high":   float(r["high"]),
                "low":    float(r["low"]),
                "close":  float(r["close"]),
                "volume": int(r.get("volume", 0)),
            })
        return bars

    @staticmethod
    def _agg_1m_to_15m(bars_1m: list[dict]) -> list:
        """Aggregate 1-min bar dicts into 15-min Candle objects for regime classification."""
        if not bars_1m:
            return []
        market_start = bars_1m[0]["timestamp"].replace(
            hour=9, minute=15, second=0, microsecond=0
        )
        candles: list[Candle] = []
        for bar in bars_1m:
            elapsed  = (bar["timestamp"] - market_start).total_seconds()
            slot_idx = max(0, int(elapsed // 900))   # 900 s = 15 min
            slot_ts  = market_start + timedelta(minutes=slot_idx * 15)
            if not candles or candles[-1].timestamp != slot_ts:
                candles.append(Candle(
                    timestamp=slot_ts,
                    open=bar["open"],  high=bar["high"],
                    low=bar["low"],    close=bar["close"],
                    volume=bar["volume"],
                ))
            else:
                c         = candles[-1]
                c.high    = max(c.high, bar["high"])
                c.low     = min(c.low,  bar["low"])
                c.close   = bar["close"]
                c.volume += bar["volume"]
        return candles

    def _replay_symbol(self, symbol: str, bars: list[dict],
                       target_date: date, results: dict,
                       nifty_map: dict | None = None,
                       nifty_open: float | None = None) -> None:
        """Replay one symbol through the ORB strategy bar-by-bar.

        Institutional enhancements applied here:
        • NIFTY direction bias  — block LONGs when NIFTY is net-negative at
          entry time; block SHORTs when NIFTY is net-positive.  Threshold ±0.2%.
          If NIFTY data is unavailable, both directions are allowed (fail-open).
        • Breakeven stop move   — after a position reaches 1R unrealised profit,
          stop is moved to entry price.  This converts many EOD SQUAREOFF losers
          into breakevens and eliminates the scenario where a 1R winner reverses
          back to a full loss.
        """
        _NIFTY_BIAS_PCT = 0.20    # % threshold for direction bias
        interval        = self._orb_tf
        market_start    = bars[0]["timestamp"].replace(hour=9, minute=15, second=0)
        candles_15m: list[Candle] = []
        orb_set       = False
        nifty_blocked = 0
        vwap_cum_pv   = 0.0       # cumulative price×volume for session VWAP
        vwap_cum_v    = 0.0       # cumulative volume
        vwap          = 0.0       # current VWAP (updated every bar)

        for bar in bars:
            elapsed  = (bar["timestamp"] - market_start).total_seconds()
            slot_idx = max(0, int(elapsed // (interval * 60)))
            slot_start = market_start + timedelta(minutes=slot_idx * interval)

            # Build / extend 15-min candle
            if not candles_15m or candles_15m[-1].timestamp != slot_start:
                if candles_15m and not orb_set:
                    self.strategy.set_opening_range(symbol, candles_15m[0])
                    self._print_orb_range(symbol, candles_15m[0])
                    orb_set = True
                candles_15m.append(Candle(
                    timestamp=slot_start,
                    open=bar["open"], high=bar["high"],
                    low=bar["low"],  close=bar["close"],
                    volume=bar["volume"],
                ))
            else:
                c = candles_15m[-1]
                c.high   = max(c.high, bar["high"])
                c.low    = min(c.low,  bar["low"])
                c.close  = bar["close"]
                c.volume += bar["volume"]

            # Update session VWAP every bar (used for dynamic target adjustment)
            vwap_cum_pv += bar["close"] * max(bar["volume"], 1)
            vwap_cum_v  += max(bar["volume"], 1)
            vwap         = vwap_cum_pv / vwap_cum_v

            if not orb_set:
                continue

            current = candles_15m[-1]

            # ── Entry signal check ────────────────────────────────────
            sig = self.strategy.evaluate(
                symbol, current, candles_15m[:-1], bar["timestamp"]
            )

            # ── NIFTY direction bias gate ─────────────────────────────
            # Principle: trade WITH the index, not against it.
            # If NIFTY is rising, fade SHORTs (fade the breakdown of a single
            # stock against a bullish index — poor-expectancy trades).
            # If NIFTY is falling, fade LONGs (same logic reversed).
            if sig and nifty_map and nifty_open:
                # Find closest NIFTY bar at or before entry time
                nifty_now = nifty_open
                for ts in sorted(nifty_map.keys()):
                    if ts <= bar["timestamp"]:
                        nifty_now = nifty_map[ts]
                    else:
                        break
                nifty_chg_pct = (nifty_now - nifty_open) / nifty_open * 100

                blocked = False
                if sig.type.name == "BUY" and nifty_chg_pct < -_NIFTY_BIAS_PCT:
                    blocked = True
                    print(f"  {_Y}  ⊘ {symbol} LONG blocked — "
                          f"NIFTY {nifty_chg_pct:+.2f}% (bearish bias){_RST}")
                elif sig.type.name == "SELL" and nifty_chg_pct > +_NIFTY_BIAS_PCT:
                    blocked = True
                    print(f"  {_Y}  ⊘ {symbol} SHORT blocked — "
                          f"NIFTY {nifty_chg_pct:+.2f}% (bullish bias){_RST}")

                if blocked:
                    # Reset ORBState so the OPPOSITE direction can still fire later
                    state = self.strategy._get_state(symbol)
                    if sig.type.name == "BUY":
                        state.long_triggered = False
                    else:
                        state.short_triggered = False
                    state.trade_taken = False
                    nifty_blocked += 1
                    sig = None

            if sig:
                # ── Sector isolation gate ─────────────────────────────────────
                # Block entry if we already hold max_sector_positions in this
                # sector.  Prevents AXISBANK + SBIN = 2× banking risk, etc.
                _sym_sec = _SECTOR_MAP.get(symbol, "UNKNOWN")
                if _sym_sec != "UNKNOWN":
                    _sec_max = ALGO_CONFIG.get("max_sector_positions", 1)
                    _sec_cnt = sum(
                        1 for t in self.risk_guard.open_positions
                        if _SECTOR_MAP.get(t.symbol, "") == _sym_sec
                    )
                    if _sec_cnt >= _sec_max:
                        print(f"  {_Y}  ⊘ {symbol} blocked — "
                              f"sector {_sym_sec} already has {_sec_cnt} position(s){_RST}")
                        sig = None

            if sig and vwap > 0:
                # ── VWAP target pull — take profit at the intraday mean ────────
                # VWAP is the strongest intraday magnet: stocks often stall and
                # reverse at VWAP before reaching the full 2R ORB target.
                # If VWAP sits between our entry and the full target, pull the
                # target to VWAP ± 0.3% (momentum buffer past the level).
                # Skip adjustment if the resulting RR would fall below 1.5.
                if sig.type.name == "BUY" and sig.price < vwap < sig.target:
                    _new_tgt = vwap * 1.003
                    _new_rr  = (_new_tgt - sig.price) / max(sig.price - sig.stop_loss, 0.01)
                    if _new_rr >= 1.5:
                        sig.target  = _new_tgt
                        sig.reason += f"  [VWAP tgt ₹{_new_tgt:.2f}]"
                        print(f"  {_D}  → {symbol} target pulled to VWAP "
                              f"₹{_new_tgt:.2f} (RR {_new_rr:.1f}×){_RST}")
                elif sig.type.name == "SELL" and sig.target < vwap < sig.price:
                    _new_tgt = vwap * 0.997
                    _new_rr  = (sig.price - _new_tgt) / max(sig.stop_loss - sig.price, 0.01)
                    if _new_rr >= 1.5:
                        sig.target  = _new_tgt
                        sig.reason += f"  [VWAP tgt ₹{_new_tgt:.2f}]"
                        print(f"  {_D}  → {symbol} target pulled to VWAP "
                              f"₹{_new_tgt:.2f} (RR {_new_rr:.1f}×){_RST}")

            if sig:
                self.executor.execute_signal(sig)

            # ── Partial exit (50%) + breakeven stop at 1R ─────────────────────
            # When unrealised P&L reaches 1× initial risk:
            #   1. Exit half the position → lock that R in trade.partial_pnl
            #   2. Move stop to entry → runner is now risk-free (worst case: BE)
            # This converts what were full-loss reversals into zero-loss breaks.
            for trade in list(self.risk_guard.open_positions):
                if trade.symbol != symbol:
                    continue
                if getattr(trade, "_be_done", False):
                    continue
                risk_1r = abs(trade.entry_price - trade.stop_loss)
                moved   = False
                if trade.is_long  and bar["high"] >= trade.entry_price + risk_1r:
                    partial_px = trade.entry_price + risk_1r
                    moved      = True
                elif not trade.is_long and bar["low"] <= trade.entry_price - risk_1r:
                    partial_px = trade.entry_price - risk_1r
                    moved      = True
                else:
                    partial_px = 0.0
                if moved:
                    trade._be_done = True
                    half_qty = trade.quantity // 2
                    if half_qty > 0:
                        p_gross           = abs(partial_px - trade.entry_price) * half_qty
                        trade.partial_pnl = p_gross
                        trade.quantity   -= half_qty
                        print(f"  {_G}  → {symbol} partial exit {half_qty} shares "
                              f"@ ₹{partial_px:.2f}  locked ₹+{p_gross:.2f}  "
                              f"({trade.quantity} running){_RST}")
                    trade.stop_loss = trade.entry_price
                    print(f"  {_G}  → {symbol} breakeven stop "
                          f"₹{trade.entry_price:.2f}{_RST}")

            # ── Exit check (SL then target, using bar extremes) ───────
            for trade in list(self.risk_guard.open_positions):
                if trade.symbol != symbol:
                    continue

                # For LONG: check low vs SL, high vs target
                # For SHORT: check high vs SL, low vs target
                sl_price  = bar["low"]  if trade.is_long else bar["high"]
                tgt_price = bar["high"] if trade.is_long else bar["low"]

                exit_reason = None
                if trade.is_long  and sl_price  <= trade.stop_loss:
                    exit_px     = trade.stop_loss
                    exit_reason = ("BREAKEVEN_STOP"
                                   if getattr(trade, "_be_done", False)
                                   else "SL_HIT")
                elif not trade.is_long and sl_price >= trade.stop_loss:
                    exit_px     = trade.stop_loss
                    exit_reason = ("BREAKEVEN_STOP"
                                   if getattr(trade, "_be_done", False)
                                   else "SL_HIT")
                elif trade.is_long  and tgt_price >= trade.target:
                    exit_px     = trade.target
                    exit_reason = "TARGET_HIT"
                elif not trade.is_long and tgt_price <= trade.target:
                    exit_px     = trade.target
                    exit_reason = "TARGET_HIT"

                if exit_reason:
                    self.executor.execute_exit(
                        trade, exit_px, bar["timestamp"], exit_reason
                    )

        # EOD square-off for remaining open positions in this symbol
        last_price = bars[-1]["close"]
        last_time  = bars[-1]["timestamp"]
        for trade in list(self.risk_guard.open_positions):
            if trade.symbol == symbol:
                self.executor.execute_exit(trade, last_price, last_time, "EOD_SQUAREOFF")

        if nifty_blocked:
            print(f"  {_D}  ({nifty_blocked} signal(s) filtered by NIFTY bias){_RST}")

        # Record result
        state = self.strategy._get_state(symbol)
        results[symbol] = {
            "range_high": state.range_high,
            "range_low":  state.range_low,
            "range_pct":  state.range_pct,
            "valid":      state.is_valid_range(),
            "traded":     state.trade_taken,
        }

    # ══════════════════════════════════════════════════════════════════════
    # STATUS / DISPLAY
    # ══════════════════════════════════════════════════════════════════════

    def print_status(self) -> None:
        summary = self.executor.get_session_summary()
        risk    = summary["risk_status"]

        print(f"\n  {_B}{'=' * 56}{_RST}")
        print(f"  {_B}  ALGO SESSION STATUS{_RST}")
        print(f"  {_B}{'=' * 56}{_RST}")

        mode_clr = _Y if summary["mode"] == "PAPER" else _G
        print(f"\n  Mode:      {mode_clr}{summary['mode']}{_RST}")
        print(f"  Strategy:  {self.strategy.NAME}")
        print(f"  Symbols:   {', '.join(self.symbols)}")

        pnl     = summary["realized_pnl"]
        pnl_clr = _G if pnl >= 0 else _R
        print(f"\n  {_B}P&L{_RST}")
        print(f"    Realized:   {pnl_clr}\u20b9{pnl:+,.2f}{_RST}")
        print(f"    Trades:     {summary['total_trades']} "
              f"(W:{summary['wins']} L:{summary['losses']})")
        print(f"    Open:       {summary['open_positions']}")

        print(f"\n  {_B}Risk Guard{_RST}")
        print(f"    Kill switch: {'ACTIVE' if risk['killed'] else 'OK'}")
        if risk["killed"]:
            print(f"    Reason:      {risk['kill_reason']}")
        print(f"    Consec loss: {risk['consecutive_losses']}")
        print(f"    Trades used: {risk['total_trades']}/{risk['max_trades']}")

        orb = self.strategy.get_status()
        if orb:
            print(f"\n  {_B}ORB Levels{_RST}")
            for sym, s in orb.items():
                if s["range_high"]:
                    valid   = f"{_G}valid{_RST}" if s["range_valid"] else f"{_R}skip{_RST}"
                    traded  = f"{_Y}TRADED{_RST}" if s["trade_taken"] else f"{_D}waiting{_RST}"
                    print(f"    {sym:12s} H:{s['range_high']:>9,.2f}  "
                          f"L:{s['range_low']:>9,.2f}  "
                          f"({s['range_pct']:.2f}%) [{valid}]  {traded}")

        print(f"\n  {_B}{'=' * 56}{_RST}\n")

    def _print_session_summary(self) -> None:
        print(f"\n  {_B}{'─' * 56}{_RST}")
        print(f"  {_B}  SESSION ENDED{_RST}")
        print(f"  {_B}{'─' * 56}{_RST}")
        pnl = self.risk_guard.realized_pnl
        clr = _G if pnl >= 0 else _R
        print(f"  Net P&L:  {clr}\u20b9{pnl:+,.2f}{_RST}")
        print(f"  Trades:   {self.risk_guard.total_trades_today} "
              f"(W:{self.risk_guard.wins} L:{self.risk_guard.losses})")
        print(f"  {_B}{'─' * 56}{_RST}\n")

    # ── Helpers ───────────────────────────────────────────────────────────

    def _get_last_trading_day(self) -> date:
        """Return the most recently *completed* NSE trading session date.

        If the current time is before market close (15:30 IST) we treat today
        as incomplete and step back one day.  We then skip over weekends and
        NSE holidays using ``NoTradeEnvironment.check_holiday``.
        """
        from algo.filters import NoTradeEnvironment
        _env = NoTradeEnvironment()

        d = date.today()
        if datetime.now().time() < time(15, 30):
            d -= timedelta(days=1)

        # Walk back until we land on a real NSE trading day (max 14 days)
        for _ in range(14):
            if d.weekday() < 5 and _env.check_holiday(d).passed:
                return d
            d -= timedelta(days=1)

        # Absolute fallback — last weekday, no holiday check
        d = date.today()
        if datetime.now().time() < time(15, 30):
            d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d

    def _print_orb_range(self, symbol: str, candle: Candle) -> None:
        rng_pct = candle.range_pct()
        valid   = (ALGO_CONFIG["orb_min_range_pct"]
                   <= rng_pct
                   <= ALGO_CONFIG["orb_max_range_pct"])
        status  = f"{_G}VALID{_RST}" if valid else f"{_R}SKIP{_RST}"
        buffer  = candle.high * (ALGO_CONFIG["orb_buffer_pct"] / 100)
        tw      = (candle.high - candle.low) * ALGO_CONFIG["orb_target_multiple"]

        print(f"\n  {_B}Opening Range{_RST}  {symbol}")
        print(f"    High: \u20b9{candle.high:,.2f}  |  Low: \u20b9{candle.low:,.2f}")
        print(f"    Width: \u20b9{candle.high - candle.low:,.2f} ({rng_pct:.2f}%)  [{status}]")
        if valid:
            print(f"    Buy above:  \u20b9{candle.high + buffer:,.2f}")
            print(f"    Sell below: \u20b9{candle.low  - buffer:,.2f}")
            print(f"    Target: \u00b1\u20b9{tw:,.2f} from entry")

    def _print_banner(self) -> None:
        mode = f"{_Y}PAPER MODE{_RST}" if self.paper_mode else f"{_R}\u26a1 LIVE MODE \u26a1{_RST}"
        print(f"\n  {_B}{'=' * 56}{_RST}")
        print(f"  {_B}  ALGO TRADING ENGINE{_RST}  \u2014  {mode}")
        print(f"  {_B}{'=' * 56}{_RST}")
        print(f"  Strategy:    {self.strategy.NAME}")
        print(f"  Instruments: {', '.join(self.symbols)}")
        print(f"  ORB Window:  09:15 \u2192 09:30 (first {self._orb_tf}min candle)")
        print(f"  Trade Hours: 09:30 \u2192 14:30")
        print(f"  Square Off:  {ALGO_CONFIG['square_off_time']}")
        print(f"  Max Loss:    \u20b9{ALGO_CONFIG['capital'] * ALGO_CONFIG['max_daily_loss_pct'] / 100:,.0f}/day")
        print(f"  Max Trades:  {ALGO_CONFIG['max_trades_per_day']}/day")
        print(f"  Kill Switch: ACTIVE")
        if not self.paper_mode:
            print(f"\n  {_R}  \u26a0  REAL MONEY MODE \u2014 orders WILL be placed on Kite{_RST}")
        print(f"  {_B}{'=' * 56}{_RST}")
        print(f"\n  Waiting for market data...\n")

    def _print_sim_banner(self, sim_date: date) -> None:
        print(f"\n  {_B}{'=' * 56}{_RST}")
        print(f"  {_B}  ORB SIMULATION{_RST}  \u2014  "
              f"{sim_date.strftime('%A, %d %B %Y')}")
        print(f"  {_B}{'=' * 56}{_RST}")
        print(f"  Strategy:    {self.strategy.NAME} ({self._orb_tf}-min opening range)")
        print(f"  Instruments: {', '.join(self.symbols)}")
        print(f"  Range valid: {ALGO_CONFIG['orb_min_range_pct']}% "
              f"\u2013 {ALGO_CONFIG['orb_max_range_pct']}%")
        print(f"  Target:      {ALGO_CONFIG['orb_target_multiple']}\u00d7 range width")
        print(f"  Risk/trade:  \u20b9{ALGO_CONFIG['max_loss_per_trade']:,}")
        print(f"  {_B}{'=' * 56}{_RST}")

    def _print_sim_summary(self, sim_date: date, results: dict) -> None:
        closed  = self.risk_guard.closed_trades
        pnl     = self.risk_guard.realized_pnl
        pnl_clr = _G if pnl >= 0 else _R

        valid_cnt  = sum(1 for r in results.values() if r.get("valid"))
        traded_cnt = sum(1 for r in results.values() if r.get("traded"))

        print(f"\n  {_B}{'=' * 56}{_RST}")
        print(f"  {_B}  SIMULATION RESULTS{_RST}  \u2014  "
              f"{sim_date.strftime('%d %b %Y')}")
        print(f"  {_B}{'=' * 56}{_RST}")
        # Count wins/losses by realized P&L — SQUAREOFF exits with profit count as wins
        wins   = sum(1 for t in closed if t.pnl > 0)
        losses = sum(1 for t in closed if t.pnl <= 0)
        be_stops = sum(1 for t in closed if t.exit_reason == "BREAKEVEN_STOP")

        print(f"\n  Stocks scanned:  {len(results)}")
        print(f"  Valid ORB range: {valid_cnt}")
        print(f"  Trades taken:    {traded_cnt}")
        print(f"  Wins:            {wins}  ({100*wins//traded_cnt if traded_cnt else 0}%)")
        print(f"  Losses:          {losses}")
        if be_stops:
            print(f"  Breakeven exits: {be_stops}  (stop moved to entry after 1R hit)")
        partial_exits = sum(1 for t in closed if t.partial_pnl > 0)
        if partial_exits:
            print(f"  Partial exits:   {partial_exits}  (50% locked at 1R, runner continued)")
        print(f"  Net P&L:         {pnl_clr}\u20b9{pnl:+,.2f}{_RST}")

        if closed:
            print(f"\n  Trade Details:")
            for t in closed:
                emoji = "\u2705" if t.pnl >= 0 else "\u274c"
                side  = "LONG " if t.is_long else "SHORT"
                clr   = _G if t.pnl >= 0 else _R
                print(f"    {emoji} {t.symbol:12s} {side}  "
                      f"\u20b9{t.entry_price:>8,.2f} \u2192 "
                      f"\u20b9{t.exit_price:>8,.2f}  "
                      f"P&L: {clr}\u20b9{t.pnl:+,.2f}{_RST}  ({t.status})")

        print(f"\n  {_B}{'=' * 56}{_RST}\n")


# ── Public API ────────────────────────────────────────────────────────────────

def run_algo(strategy: str = "ORB", live: bool = False,
             simulate: bool = False, status: bool = False,
             journal_dir: str = "journal",
             auto_select: bool = True, top_n: int = 8) -> None:
    """
    Entry point called from run.py.

    FROZEN: --algo --live is permanently disabled in production hardening
    pass v1. The intraday ORB algo is a RESEARCH ARTIFACT only — its edge
    has not been statistically validated. Only --simulate and --status are
    permitted.

    To unfreeze, all of the following must be true:
      1. python run.py --walkforward 180 returns "EDGE VALIDATED"
      2. 60 consecutive paper-mode trades, PF >= 2.0, max DD < 5%
      3. Explicit human sign-off recorded in Journal/ALGO_LIVE_APPROVED.txt
    """
    if live:
        raise NotImplementedError(
            "FROZEN: --algo --live is disabled by production hardening.\n"
            "  The intraday ORB algo has not been validated for real capital.\n"
            "  Use --algo --simulate for research, --backtest / --walkforward "
            "for validation.\n"
            "  See run_algo() docstring for unfreeze requirements."
        )

    paper_mode = not live

    selected_symbols: list[str] | None = None
    if auto_select and not status:
        selected_symbols = _run_startup_screener(top_n=top_n)

    engine = AlgoEngine(strategy_name=strategy, paper_mode=paper_mode,
                        journal_dir=journal_dir, symbols=selected_symbols)

    if status:
        engine.print_status()
    elif simulate:
        engine.run_simulate()
    else:
        # paper mode — allowed, doesn't touch broker
        engine.run_live()


def _run_startup_screener(top_n: int = 8) -> list[str]:
    """
    Run the instrument screener once at engine startup to build today's
    trading universe.  Downloads 15-min data for all 27 candidates,
    scores each one, and returns the symbols with score > 0 (min 4).
    """
    from algo.backtest import Backtester
    from algo.screener import CANDIDATE_POOL, screen_candidates, print_screening_report

    _B, _D, _R, _RST = "\033[1m", "\033[2m", "\033[91m", "\033[0m"

    print(f"\n  {_B}Screener: selecting today's trading universe…{_RST}")
    bt = Backtester(days=30)

    all_data: dict = {}
    for sym in CANDIDATE_POOL:
        bars = bt._download(sym)
        if bars:
            all_data[sym] = bars

    nifty_data = bt._download_nifty()
    all_dates  = sorted({d for v in all_data.values() for d in v})

    selected, scores = screen_candidates(
        bt._replay_day, all_data, nifty_data,
        all_dates, Backtester.FILTERED,
        top_n=top_n, min_score=0.0, min_floor=4,
    )
    print_screening_report(scores, top_n=top_n, min_score=0.0)
    return selected
