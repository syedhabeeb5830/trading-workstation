"""
algo/engine.py — Algo Trading Engine (Main Event Loop)
========================================================
Three execution modes:
  1. LIVE    — KiteTicker WebSocket → real-time strategy evaluation → orders
  2. PAPER   — Same as LIVE but executor logs instead of placing orders
  3. SIMULATE — Replay historical intraday data through strategy (instant)

Architecture:
  KiteTicker → CandleAggregator → Strategy.evaluate() → RiskGuard → Executor

Usage:
  python run.py --algo                   # paper mode (default, safe)
  python run.py --algo --live            # real orders
  python run.py --algo --simulate        # replay today's data
  python run.py --algo --status          # show session P&L
"""

from __future__ import annotations
import signal
import sys
import time as time_module
from datetime import datetime, time, date, timedelta
from typing import Optional

from algo.config import ALGO_CONFIG
from algo.candles import MultiTimeframeCandleManager, Candle
from algo.strategies import get_strategy, Signal, SignalType, ORBStrategy
from algo.risk_guard import RiskGuard, TradeRecord
from algo.executor import AlgoExecutor


# ── Colour helpers ────────────────────────────────────────────────────────────
_G, _Y, _R, _C, _B, _D, _RST = (
    "\033[92m", "\033[93m", "\033[91m",
    "\033[96m", "\033[1m", "\033[2m", "\033[0m"
)


class AlgoEngine:
    """
    Core algo trading engine. Manages the full lifecycle:
    connect → receive ticks → build candles → evaluate strategy →
    check risk → execute → monitor → square off.
    """

    def __init__(self, strategy_name: str = "ORB", paper_mode: bool = True,
                 journal_dir: str = "journal"):
        self.paper_mode = paper_mode
        self.journal_dir = journal_dir
        self.strategy = get_strategy(strategy_name)
        self.risk_guard = RiskGuard(ALGO_CONFIG)
        self.executor = AlgoExecutor(self.risk_guard, paper_mode, journal_dir)
        self.symbols = ALGO_CONFIG["instruments"]
        self.candle_manager = MultiTimeframeCandleManager(
            symbols=self.symbols,
            intervals=ALGO_CONFIG["candle_intervals"],
            on_candle_close=self._on_candle_close,
        )
        self._running = False
        self._orb_timeframe = ALGO_CONFIG["orb_timeframe_minutes"]
        self._square_off_time = time(*map(int, ALGO_CONFIG["square_off_time"].split(":")))

    # ══════════════════════════════════════════════════════════════════════════
    # MODE 1: LIVE / PAPER  (KiteTicker WebSocket)
    # ══════════════════════════════════════════════════════════════════════════

    def run_live(self) -> None:
        """
        Start the live algo engine with KiteTicker.
        Blocks until market close or kill switch.
        """
        self._print_banner()
        self._running = True

        # Graceful shutdown on Ctrl+C
        signal.signal(signal.SIGINT, self._handle_shutdown)

        try:
            from algo.instruments import resolve_tokens
            from integrations.zerodha import get_kite

            kite = get_kite()
            token_map = resolve_tokens(self.symbols)

            if not token_map:
                print(f"\n  {_R}✗ Could not resolve instrument tokens.{_RST}")
                print(f"  {_D}  Ensure Kite session is active (--kite-login){_RST}\n")
                return

            # Map tokens back to symbols for tick routing
            self._token_to_symbol = {v: k for k, v in token_map.items()}
            tokens = list(token_map.values())

            print(f"  Subscribing to {len(tokens)} instruments...")
            for sym, tok in token_map.items():
                print(f"    {sym}: {tok}")

            # Start KiteTicker
            from kiteconnect import KiteTicker
            kws = KiteTicker(kite.api_key, kite.access_token)

            kws.on_ticks = self._on_ticks
            kws.on_connect = lambda ws, response: ws.subscribe(tokens)
            kws.on_connect = lambda ws, response: (
                ws.subscribe(tokens),
                ws.set_mode(ws.MODE_FULL, tokens),
            )
            kws.on_close = self._on_ws_close
            kws.on_error = self._on_ws_error

            print(f"\n  {_G}● ENGINE RUNNING{_RST}  "
                  f"{'PAPER' if self.paper_mode else 'LIVE'} mode\n")

            kws.connect(threaded=True)

            # Main loop: check for square-off time and kill switch
            while self._running:
                now = datetime.now()
                if now.time() >= self._square_off_time:
                    self._square_off_all()
                    break
                if self.risk_guard.is_killed:
                    self._square_off_all()
                    break
                # Check open positions for SL/Target hits
                self._check_exits(now)
                time_module.sleep(1)

            kws.close()

        except ImportError as e:
            print(f"\n  {_R}✗ Missing dependency: {e}{_RST}")
            print(f"  {_D}  pip install kiteconnect{_RST}\n")
        except Exception as e:
            print(f"\n  {_R}✗ Engine error: {e}{_RST}\n")
        finally:
            self._print_session_summary()

    def _on_ticks(self, ws, ticks: list) -> None:
        """KiteTicker tick callback. Routes ticks to candle builders."""
        for tick in ticks:
            token = tick.get("instrument_token")
            symbol = self._token_to_symbol.get(token)
            if not symbol:
                continue

            price = tick.get("last_price", 0)
            volume = tick.get("volume_traded", 0)
            tick_time = tick.get("exchange_timestamp", datetime.now())

            if isinstance(tick_time, str):
                tick_time = datetime.fromisoformat(tick_time)

            # Feed to candle builders
            self.candle_manager.on_tick(symbol, price, volume, tick_time)

            # Also check exits on every tick for faster response
            self._check_tick_exit(symbol, price, tick_time)

    def _on_candle_close(self, symbol: str, interval: int,
                         candle: Candle, candles: list[Candle]) -> None:
        """Called when a candle completes. Evaluates strategy."""
        if interval != self._orb_timeframe:
            return  # Only evaluate on the ORB timeframe

        now = candle.timestamp + timedelta(minutes=interval)

        # Evaluate strategy
        sig = self.strategy.evaluate(symbol, candle, candles, now)
        if sig:
            self.executor.execute_signal(sig)

    def _check_tick_exit(self, symbol: str, price: float, tick_time: datetime) -> None:
        """Check if any open position hit SL or target on this tick."""
        for trade in self.risk_guard.open_positions:
            if trade.symbol != symbol:
                continue
            exit_signal = self.strategy.check_exit(
                symbol, price, trade.entry_price,
                trade.stop_loss, trade.target,
                trade.is_long, tick_time,
            )
            if exit_signal:
                self.executor.execute_exit(trade, price, tick_time, exit_signal.reason)

    def _check_exits(self, now: datetime) -> None:
        """Periodic exit check (backup for tick-level checks)."""
        # In live mode, exits are handled by _check_tick_exit
        # This is just a safety net
        pass

    def _on_ws_close(self, ws, code, reason) -> None:
        if self._running:
            print(f"\n  {_Y}⚠ WebSocket closed: {reason}. Reconnecting...{_RST}")

    def _on_ws_error(self, ws, code, reason) -> None:
        print(f"  {_R}WebSocket error: {code} — {reason}{_RST}")

    def _handle_shutdown(self, signum, frame) -> None:
        """Graceful shutdown on Ctrl+C — square off then exit."""
        print(f"\n\n  {_Y}⚠ SHUTDOWN SIGNAL — squaring off all positions...{_RST}")
        self._square_off_all()
        self._running = False

    def _square_off_all(self) -> None:
        """Square off all open positions at current prices."""
        open_positions = self.risk_guard.open_positions
        if not open_positions:
            print(f"  {_D}No open positions to square off.{_RST}")
            return

        # In paper mode, use last known prices
        prices = {}
        for trade in open_positions:
            current = self.candle_manager.get_current(trade.symbol, self._orb_timeframe)
            if current:
                prices[trade.symbol] = current.close
            else:
                prices[trade.symbol] = trade.entry_price  # fallback

        closed = self.executor.execute_square_off(prices, datetime.now())
        if closed:
            print(f"\n  {_B}Squared off {len(closed)} position(s){_RST}")

    # ══════════════════════════════════════════════════════════════════════════
    # MODE 3: SIMULATE  (Replay historical data)
    # ══════════════════════════════════════════════════════════════════════════

    def run_simulate(self, sim_date: Optional[date] = None) -> None:
        """
        Replay intraday data through the strategy engine.
        Downloads 1-min data from yfinance. Builds candles directly (no threads).
        """
        import yfinance as yf
        import pandas as pd
        from algo.candles import Candle

        # Disable Telegram for simulation
        self.executor._sim_mode = True

        target_date = sim_date or self._get_last_trading_day()
        self._print_sim_banner(target_date)

        results = {}

        for symbol in self.symbols:
            ticker = f"{symbol}.NS"
            print(f"\n  {_B}{'─' * 56}{_RST}")
            print(f"  {_C}{symbol}{_RST}  — downloading 1-min data...", flush=True)

            try:
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
                    print(f"  {_D}  No data available for {target_date}{_RST}")
                    continue

                # Convert to bar list
                bars = []
                for idx, row in df.iterrows():
                    ts = idx.to_pydatetime()
                    if hasattr(ts, 'tz') and ts.tzinfo:
                        ts = ts.replace(tzinfo=None)
                    bars.append({
                        "timestamp": ts,
                        "open": float(row["Open"]),
                        "high": float(row["High"]),
                        "low": float(row["Low"]),
                        "close": float(row["Close"]),
                        "volume": int(row.get("Volume", 0)),
                    })

                if not bars:
                    print(f"  {_D}  No bars after filtering{_RST}")
                    continue

                print(f"  {_D}  {len(bars)} bars loaded "
                      f"({bars[0]['timestamp'].strftime('%H:%M')} → "
                      f"{bars[-1]['timestamp'].strftime('%H:%M')}){_RST}", flush=True)

                # ── Build 15-min candles from 1-min bars (simple, no threads) ──
                interval = self._orb_timeframe
                market_start = bars[0]["timestamp"].replace(hour=9, minute=15, second=0)
                candles_15m: list[Candle] = []
                orb_set = False

                for bar in bars:
                    # Determine which 15-min slot this bar belongs to
                    elapsed = (bar["timestamp"] - market_start).total_seconds()
                    slot_idx = int(elapsed // (interval * 60))
                    slot_start = market_start + timedelta(minutes=slot_idx * interval)

                    # Build/extend candle for this slot
                    if not candles_15m or candles_15m[-1].timestamp != slot_start:
                        # New candle slot — close previous if exists
                        if candles_15m and not orb_set:
                            # First candle just completed = opening range
                            self.strategy.set_opening_range(symbol, candles_15m[0])
                            self._print_orb_range(symbol, candles_15m[0])
                            orb_set = True
                        candles_15m.append(Candle(
                            timestamp=slot_start,
                            open=bar["open"], high=bar["high"],
                            low=bar["low"], close=bar["close"],
                            volume=bar["volume"],
                        ))
                    else:
                        c = candles_15m[-1]
                        c.high = max(c.high, bar["high"])
                        c.low = min(c.low, bar["low"])
                        c.close = bar["close"]
                        c.volume += bar["volume"]

                    # ── Strategy evaluation (after ORB is set) ────────────
                    if orb_set:
                        current = candles_15m[-1]
                        sig = self.strategy.evaluate(
                            symbol, current, candles_15m[:-1], bar["timestamp"]
                        )
                        if sig:
                            self.executor.execute_signal(sig)

                        # Check exits using bar HIGH and LOW (not just close)
                        for trade in list(self.risk_guard.open_positions):
                            if trade.symbol != symbol:
                                continue
                            # Check LOW for long stop / HIGH for short stop
                            check_price = bar["low"] if trade.is_long else bar["high"]
                            exit_sig = self.strategy.check_exit(
                                symbol, check_price, trade.entry_price,
                                trade.stop_loss, trade.target,
                                trade.is_long, bar["timestamp"],
                            )
                            if not exit_sig:
                                # Also check target with HIGH for long / LOW for short
                                target_price = bar["high"] if trade.is_long else bar["low"]
                                exit_sig = self.strategy.check_exit(
                                    symbol, target_price, trade.entry_price,
                                    trade.stop_loss, trade.target,
                                    trade.is_long, bar["timestamp"],
                                )
                            if exit_sig:
                                exit_px = (bar["low"] if trade.is_long and
                                           bar["low"] <= trade.stop_loss
                                           else bar["high"] if not trade.is_long and
                                           bar["high"] >= trade.stop_loss
                                           else exit_sig.price)
                                self.executor.execute_exit(
                                    trade, exit_px,
                                    bar["timestamp"], exit_sig.reason
                                )

                # EOD: square off any remaining for this symbol
                last_price = bars[-1]["close"]
                last_time = bars[-1]["timestamp"]
                for trade in list(self.risk_guard.open_positions):
                    if trade.symbol == symbol:
                        self.executor.execute_exit(
                            trade, last_price, last_time, "EOD_SQUAREOFF"
                        )

                # Record result
                state = self.strategy._get_state(symbol)
                results[symbol] = {
                    "range_high": state.range_high,
                    "range_low": state.range_low,
                    "range_pct": state.range_pct,
                    "valid": state.is_valid_range(),
                    "traded": state.trade_taken,
                }

            except Exception as e:
                import traceback
                print(f"  {_R}  Error: {e}{_RST}")
                traceback.print_exc()
                continue

        # Print simulation summary
        self._print_sim_summary(target_date, results)

    def _get_last_trading_day(self) -> date:
        """Get the most recent trading day (skip weekends)."""
        d = date.today()
        # If it's before market close today, use yesterday
        if datetime.now().time() < time(15, 30):
            d -= timedelta(days=1)
        # Skip weekends
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d

    def _print_orb_range(self, symbol: str, candle: Candle) -> None:
        """Print the opening range once established."""
        range_pct = candle.range_pct()
        valid = ALGO_CONFIG["orb_min_range_pct"] <= range_pct <= ALGO_CONFIG["orb_max_range_pct"]
        status = f"{_G}VALID{_RST}" if valid else f"{_R}SKIP{_RST}"

        print(f"\n  {_B}Opening Range{_RST}  {symbol}")
        print(f"    High: ₹{candle.high:,.2f}  |  Low: ₹{candle.low:,.2f}")
        print(f"    Width: ₹{candle.high - candle.low:,.2f} ({range_pct:.2f}%)  [{status}]")

        if valid:
            buffer = candle.high * (ALGO_CONFIG["orb_buffer_pct"] / 100)
            print(f"    Buy above:  ₹{candle.high + buffer:,.2f}")
            print(f"    Sell below: ₹{candle.low - buffer:,.2f}")
            target_width = (candle.high - candle.low) * ALGO_CONFIG["orb_target_multiple"]
            print(f"    Target: ±₹{target_width:,.2f} from entry")

    # ══════════════════════════════════════════════════════════════════════════
    # STATUS DISPLAY
    # ══════════════════════════════════════════════════════════════════════════

    def print_status(self) -> None:
        """Print current session status (for --algo --status)."""
        import json
        from pathlib import Path

        print(f"\n  {_B}{'═' * 56}{_RST}")
        print(f"  {_B}  ALGO TRADING SESSION STATUS{_RST}")
        print(f"  {_B}{'═' * 56}{_RST}")

        summary = self.executor.get_session_summary()
        risk = summary["risk_status"]

        mode_clr = _Y if summary["mode"] == "PAPER" else _G
        print(f"\n  Mode:      {mode_clr}{summary['mode']}{_RST}")
        print(f"  Strategy:  {self.strategy.NAME}")
        print(f"  Symbols:   {', '.join(self.symbols)}")

        pnl = summary["realized_pnl"]
        pnl_clr = _G if pnl >= 0 else _R
        print(f"\n  {_B}P&L{_RST}")
        print(f"    Realized:   {pnl_clr}₹{pnl:+,.2f}{_RST}")
        print(f"    Trades:     {summary['total_trades']} "
              f"(W:{summary['wins']} L:{summary['losses']})")
        print(f"    Open:       {summary['open_positions']}")

        print(f"\n  {_B}Risk Guard{_RST}")
        print(f"    Kill switch: {'🔴 ACTIVE' if risk['killed'] else '🟢 OK'}")
        if risk["killed"]:
            print(f"    Reason:      {risk['kill_reason']}")
        print(f"    Paused:      {'Yes' if risk['paused'] else 'No'}")
        print(f"    Consec loss: {risk['consecutive_losses']}")
        print(f"    Trades used: {risk['total_trades']}/{risk['max_trades']}")
        print(f"    Daily limit: ₹{risk['max_daily_loss']:,.0f} loss / "
              f"₹{risk['max_daily_profit']:,.0f} profit")

        # Show ORB states
        orb_status = self.strategy.get_status()
        if orb_status:
            print(f"\n  {_B}ORB Levels{_RST}")
            for sym, s in orb_status.items():
                if s["range_high"]:
                    valid = f"{_G}✓{_RST}" if s["range_valid"] else f"{_R}✗{_RST}"
                    traded = f"{_Y}TRADED{_RST}" if s["trade_taken"] else f"{_D}waiting{_RST}"
                    print(f"    {sym:12s} H:{s['range_high']:>9,.2f}  "
                          f"L:{s['range_low']:>9,.2f}  "
                          f"({s['range_pct']:.2f}%) {valid}  {traded}")

        print(f"\n  {_B}{'═' * 56}{_RST}\n")

    # ── Banners ───────────────────────────────────────────────────────────────

    def _print_banner(self) -> None:
        mode = f"{_Y}PAPER MODE{_RST}" if self.paper_mode else f"{_R}⚡ LIVE MODE ⚡{_RST}"
        print(f"\n  {_B}{'═' * 56}{_RST}")
        print(f"  {_B}  ALGO TRADING ENGINE{_RST}  —  {mode}")
        print(f"  {_B}{'═' * 56}{_RST}")
        print(f"  Strategy:    {self.strategy.NAME}")
        print(f"  Instruments: {', '.join(self.symbols)}")
        print(f"  ORB Window:  09:15 → 09:30 (first {self._orb_timeframe}min candle)")
        print(f"  Trade Hours: 09:30 → 14:30")
        print(f"  Square Off:  {ALGO_CONFIG['square_off_time']}")
        print(f"  Max Loss:    ₹{ALGO_CONFIG['capital'] * ALGO_CONFIG['max_daily_loss_pct'] / 100:,.0f}/day")
        print(f"  Max Trades:  {ALGO_CONFIG['max_trades_per_day']}/day")
        print(f"  Kill Switch: ACTIVE")
        if not self.paper_mode:
            print(f"\n  {_R}  ⚠  REAL MONEY MODE — orders WILL be placed on Kite{_RST}")
        print(f"  {_B}{'═' * 56}{_RST}")
        print(f"\n  Waiting for market data...\n")

    def _print_sim_banner(self, sim_date: date) -> None:
        print(f"\n  {_B}{'═' * 56}{_RST}")
        print(f"  {_B}  ORB SIMULATION{_RST}  —  {sim_date.strftime('%A, %d %B %Y')}")
        print(f"  {_B}{'═' * 56}{_RST}")
        print(f"  Strategy:    {self.strategy.NAME} ({self._orb_timeframe}-min opening range)")
        print(f"  Instruments: {', '.join(self.symbols)}")
        print(f"  Range valid: {ALGO_CONFIG['orb_min_range_pct']}% – {ALGO_CONFIG['orb_max_range_pct']}%")
        print(f"  Target:      {ALGO_CONFIG['orb_target_multiple']}× range width")
        print(f"  Risk/trade:  ₹{ALGO_CONFIG['max_loss_per_trade']:,}")
        print(f"  {_B}{'═' * 56}{_RST}")

    def _print_sim_summary(self, sim_date: date, results: dict) -> None:
        """Print end-of-simulation summary."""
        print(f"\n  {_B}{'═' * 56}{_RST}")
        print(f"  {_B}  SIMULATION RESULTS{_RST}  —  {sim_date.strftime('%d %b %Y')}")
        print(f"  {_B}{'═' * 56}{_RST}")

        # Strategy summary
        closed = self.risk_guard.closed_trades
        pnl = self.risk_guard.realized_pnl
        pnl_clr = _G if pnl >= 0 else _R

        valid_count = sum(1 for r in results.values() if r.get("valid"))
        traded_count = sum(1 for r in results.values() if r.get("traded"))

        print(f"\n  Stocks scanned:  {len(results)}")
        print(f"  Valid ORB range: {valid_count}")
        print(f"  Trades taken:    {traded_count}")
        print(f"  Wins:            {self.risk_guard.wins}")
        print(f"  Losses:          {self.risk_guard.losses}")
        print(f"  Net P&L:         {pnl_clr}₹{pnl:+,.2f}{_RST}")

        if closed:
            print(f"\n  {_B}Trade Details:{_RST}")
            for t in closed:
                emoji = "✅" if t.pnl >= 0 else "❌"
                direction = "LONG" if t.is_long else "SHORT"
                print(f"    {emoji} {t.symbol:12s} {direction:5s}  "
                      f"₹{t.entry_price:>8,.2f} → ₹{t.exit_price:>8,.2f}  "
                      f"P&L: {_G if t.pnl >= 0 else _R}₹{t.pnl:+,.2f}{_RST}  "
                      f"({t.status})")

        if not closed and not traded_count:
            print(f"\n  {_D}No trades triggered. This happens when:{_RST}")
            print(f"  {_D}  • Opening ranges are too wide (>1.5%) or too narrow (<0.3%){_RST}")
            print(f"  {_D}  • Price didn't break the range during trading hours{_RST}")
            print(f"  {_D}  • All valid breaks happened after 14:30 cutoff{_RST}")

        print(f"\n  {_B}{'═' * 56}{_RST}\n")

    def _print_session_summary(self) -> None:
        """Print when engine stops."""
        pnl = self.risk_guard.realized_pnl
        pnl_clr = _G if pnl >= 0 else _R
        print(f"\n  {_B}SESSION ENDED{_RST}")
        print(f"  Trades: {self.risk_guard.total_trades_today}  "
              f"(W:{self.risk_guard.wins} L:{self.risk_guard.losses})  "
              f"P&L: {pnl_clr}₹{pnl:+,.2f}{_RST}")
        if self.risk_guard.is_killed:
            print(f"  {_R}Kill switch was activated.{_RST}")
        print()


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API  (called from run.py)
# ══════════════════════════════════════════════════════════════════════════════

def run_algo(strategy: str = "ORB", paper_mode: bool = True,
             simulate: bool = False, status: bool = False,
             journal_dir: str = "journal") -> None:
    """Main entry point for algo trading."""
    engine = AlgoEngine(
        strategy_name=strategy,
        paper_mode=paper_mode,
        journal_dir=journal_dir,
    )

    if status:
        engine.print_status()
    elif simulate:
        engine.run_simulate()
    else:
        engine.run_live()
