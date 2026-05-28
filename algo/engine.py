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


class AlgoEngine:
    """Core algo engine — orchestrates candle building, strategy, risk, execution."""

    def __init__(self, strategy_name: str = "ORB", paper_mode: bool = True,
                 journal_dir: str = "journal"):
        self.paper_mode    = paper_mode
        self.journal_dir   = journal_dir
        self.strategy      = get_strategy(strategy_name)
        self.risk_guard    = RiskGuard(ALGO_CONFIG)
        self.executor      = AlgoExecutor(self.risk_guard, paper_mode, journal_dir)
        self.symbols       = list(ALGO_CONFIG["instruments"])
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
        Replay yesterday's 1-min intraday data through the strategy.
        Uses yfinance for data. Falls back to Kite historical API if yfinance fails.
        """
        import yfinance as yf
        import pandas as pd

        self.executor._sim_mode = True

        target_date = sim_date or self._get_last_trading_day()
        self._print_sim_banner(target_date)

        results: dict[str, dict] = {}

        for symbol in self.symbols:
            print(f"\n  {_B}{'─' * 56}{_RST}")
            print(f"  {_C}{symbol}{_RST}  \u2014 downloading 1-min data...", flush=True)

            bars = self._fetch_bars(symbol, target_date)

            if not bars:
                print(f"  {_D}  No data available for {target_date}{_RST}")
                results[symbol] = {"valid": False, "traded": False}
                continue

            print(f"  {_D}  {len(bars)} bars loaded "
                  f"({bars[0]['timestamp'].strftime('%H:%M')} \u2192 "
                  f"{bars[-1]['timestamp'].strftime('%H:%M')}){_RST}", flush=True)

            self._replay_symbol(symbol, bars, target_date, results)

        self._print_sim_summary(target_date, results)

    def _fetch_bars(self, symbol: str, target_date: date) -> list[dict]:
        """Download 1-min bars, trying yfinance first then Kite."""
        try:
            return self._fetch_yfinance(symbol, target_date)
        except Exception:
            pass
        try:
            return self._fetch_kite(symbol, target_date)
        except Exception:
            return []

    def _fetch_yfinance(self, symbol: str, target_date: date) -> list[dict]:
        import yfinance as yf
        import pandas as pd

        ticker = f"{symbol}.NS"
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
            bars.append({
                "timestamp": ts,
                "open":   float(row["Open"]),
                "high":   float(row["High"]),
                "low":    float(row["Low"]),
                "close":  float(row["Close"]),
                "volume": int(row.get("Volume", 0)),
            })
        return bars

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

    def _replay_symbol(self, symbol: str, bars: list[dict],
                       target_date: date, results: dict) -> None:
        """Replay one symbol through the ORB strategy bar-by-bar."""
        interval     = self._orb_tf
        market_start = bars[0]["timestamp"].replace(hour=9, minute=15, second=0)
        candles_15m: list[Candle] = []
        orb_set = False

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

            if not orb_set:
                continue

            current = candles_15m[-1]

            # ── Entry signal check ────────────────────────────────────
            sig = self.strategy.evaluate(
                symbol, current, candles_15m[:-1], bar["timestamp"]
            )
            if sig:
                self.executor.execute_signal(sig)

            # ── Exit check (SL then target, using bar extremes) ───────
            for trade in list(self.risk_guard.open_positions):
                if trade.symbol != symbol:
                    continue

                # For LONG: check low vs SL, high vs target
                # For SHORT: check high vs SL, low vs target
                sl_price  = bar["low"]  if trade.is_long else bar["high"]
                tgt_price = bar["high"] if trade.is_long else bar["low"]

                exit_sig = self.strategy.check_exit(
                    symbol, sl_price, trade.entry_price,
                    trade.stop_loss, trade.target, trade.is_long, bar["timestamp"],
                )
                if not exit_sig:
                    exit_sig = self.strategy.check_exit(
                        symbol, tgt_price, trade.entry_price,
                        trade.stop_loss, trade.target, trade.is_long, bar["timestamp"],
                    )
                if exit_sig:
                    # Determine exact exit price
                    if trade.is_long and sl_price <= trade.stop_loss:
                        exit_px = trade.stop_loss
                    elif not trade.is_long and sl_price >= trade.stop_loss:
                        exit_px = trade.stop_loss
                    elif trade.is_long and tgt_price >= trade.target:
                        exit_px = trade.target
                    elif not trade.is_long and tgt_price <= trade.target:
                        exit_px = trade.target
                    else:
                        exit_px = exit_sig.price

                    self.executor.execute_exit(
                        trade, exit_px, bar["timestamp"], exit_sig.reason
                    )

        # EOD square-off for remaining open positions in this symbol
        last_price = bars[-1]["close"]
        last_time  = bars[-1]["timestamp"]
        for trade in list(self.risk_guard.open_positions):
            if trade.symbol == symbol:
                self.executor.execute_exit(trade, last_price, last_time, "EOD_SQUAREOFF")

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
        print(f"\n  Stocks scanned:  {len(results)}")
        print(f"  Valid ORB range: {valid_cnt}")
        print(f"  Trades taken:    {traded_cnt}")
        print(f"  Wins:            {self.risk_guard.wins}")
        print(f"  Losses:          {self.risk_guard.losses}")
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
             journal_dir: str = "journal") -> None:
    """
    Entry point called from run.py.
    """
    paper_mode = not live
    engine = AlgoEngine(strategy_name=strategy, paper_mode=paper_mode,
                        journal_dir=journal_dir)

    if status:
        engine.print_status()
    elif simulate:
        engine.run_simulate()
    else:
        engine.run_live()
