"""
algo/backtest.py — ORB Multi-Day Backtest Engine
=================================================
Downloads 15-min OHLCV data for the last N trading days from yfinance,
replays the ORB strategy through each day, and applies the full
Zerodha MIS fee model (brokerage + STT + exchange + GST + SEBI + stamp).

Key design decisions:
  - Uses 15-min bars (yfinance provides up to 60 calendar days free)
  - Entry: close price of the breakout candle
  - Exit within candle: SL takes priority over target (conservative)
  - State resets every morning (fresh ORB range, fresh daily limits)
  - Runs two configs side-by-side: Current vs Tightened
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional

from algo.config import ALGO_CONFIG
from algo.candles import Candle
from algo.filters import (
    BreakoutQualityFilter, LiquiditySweepDetector,
    RelativeStrengthFilter, TimeWindowFilter, GapFilter,
    PDHProximityFilter, FilterResult, NoTradeEnvironment,
    VolumeConfirmationFilter, OrbVolumeGate,
    run_gate,
)


# ── ANSI colours ──────────────────────────────────────────────────────────────
_G, _Y, _R, _C, _B, _D, _RST = (
    "\033[92m", "\033[93m", "\033[91m",
    "\033[96m", "\033[1m", "\033[2m", "\033[0m",
)


# ─────────────────────────────────────────────────────────────────────────────
# ZERODHA MIS FEE MODEL  (NSE equity intraday, verified against brokerage calc)
# ─────────────────────────────────────────────────────────────────────────────
class FeeModel:
    """
    Zerodha MIS (intraday equity, NSE) full fee breakdown.

    Per round-trip trade components:
      Brokerage     : 0.03% or ₹20 per order (whichever lower), both sides
      STT           : 0.025% on SELL-side turnover only (MIS equity)
      NSE Txn charge: 0.00297% per side
      Clearing      : 0.0002% per side
      SEBI turnover : 0.0001% per side
      Stamp duty    : 0.003% on BUY-side only
      GST           : 18% on (brokerage + exchange + clearing)
    """

    BROKERAGE_MAX  = 20.00       # ₹ max per order
    BROKERAGE_PCT  = 0.0003      # 0.03% per order
    STT_SELL       = 0.00025     # 0.025% on sell-side turnover (MIS)
    NSE_TXN        = 0.0000297   # 0.00297% per side (NSE transaction)
    CLEARING       = 0.0000020   # 0.00020% per side
    SEBI           = 0.0000010   # 0.00010% per side
    STAMP_BUY      = 0.000015    # 0.0015% on buy-side (intraday equity)
    GST            = 0.18        # 18% on brokerage + exchange + clearing

    @classmethod
    def calculate(cls, entry_px: float, exit_px: float, qty: int) -> float:
        """Total round-trip cost in ₹ for one MIS trade."""
        buy_val   = entry_px * qty
        sell_val  = exit_px  * qty
        total_val = buy_val + sell_val

        brk_buy   = min(cls.BROKERAGE_MAX, cls.BROKERAGE_PCT * buy_val)
        brk_sell  = min(cls.BROKERAGE_MAX, cls.BROKERAGE_PCT * sell_val)
        brokerage = brk_buy + brk_sell

        stt       = cls.STT_SELL  * sell_val
        exchange  = cls.NSE_TXN   * total_val
        clearing  = cls.CLEARING  * total_val
        sebi      = cls.SEBI      * total_val
        stamp     = cls.STAMP_BUY * buy_val
        gst       = cls.GST       * (brokerage + exchange + clearing)

        return round(brokerage + stt + exchange + clearing + sebi + stamp + gst, 2)

    @classmethod
    def breakdown(cls, entry_px: float, exit_px: float, qty: int) -> dict:
        """Itemised fee breakdown for display."""
        buy_val   = entry_px * qty
        sell_val  = exit_px  * qty
        total_val = buy_val + sell_val
        brk_buy   = min(cls.BROKERAGE_MAX, cls.BROKERAGE_PCT * buy_val)
        brk_sell  = min(cls.BROKERAGE_MAX, cls.BROKERAGE_PCT * sell_val)
        brokerage = brk_buy + brk_sell
        exchange  = cls.NSE_TXN  * total_val
        clearing  = cls.CLEARING * total_val
        return {
            "brokerage": round(brokerage, 2),
            "stt":       round(cls.STT_SELL  * sell_val, 2),
            "exchange":  round(exchange,                 2),
            "clearing":  round(clearing,                 2),
            "sebi":      round(cls.SEBI * total_val,     2),
            "stamp":     round(cls.STAMP_BUY * buy_val,  2),
            "gst":       round(cls.GST * (brokerage + exchange + clearing), 2),
        }


# ─────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class BacktestTrade:
    trade_date:  date
    symbol:      str
    direction:   str          # LONG / SHORT
    entry_time:  datetime
    entry_px:    float
    stop_px:     float
    target_px:   float
    exit_time:   datetime
    exit_px:     float
    qty:         int
    gross_pnl:   float
    fees:        float
    net_pnl:     float
    exit_reason: str          # SL / T1+T2 / T1+BE / T1+EOD / EOD
    range_pct:   float        # ORB range width %
    t1_hit:          bool  = False      # first target was reached
    t1_px:           float = 0.0        # price at which T1 was hit
    initial_stop_px: float = 0.0        # stop before breakeven move (for R-multiple)
    regime:          str   = "UNKNOWN"  # TREND_UP / TREND_DOWN / CHOP / UNKNOWN

    @property
    def is_win(self) -> bool:
        return self.net_pnl > 0

    @property
    def r_multiple(self) -> float:
        risk = abs(self.entry_px - self.stop_px) * self.qty
        return round(self.net_pnl / risk, 2) if risk > 0 else 0.0


@dataclass
class DayResult:
    date:           date
    trades:         list[BacktestTrade] = field(default_factory=list)
    skipped:        int = 0    # stocks with invalid ORB range
    rejected:       int = 0    # signals blocked by kill switch
    filter_blocked: int  = 0      # signals blocked by quality / environment filters
    env_blocked:    bool = False   # entire day blocked by NoTradeEnvironment gate
    regime:         str  = "UNKNOWN"  # market regime for this day

    @property
    def gross_pnl(self) -> float:
        return sum(t.gross_pnl for t in self.trades)

    @property
    def fees(self) -> float:
        return sum(t.fees for t in self.trades)

    @property
    def net_pnl(self) -> float:
        return sum(t.net_pnl for t in self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.net_pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.net_pnl <= 0)


# ─────────────────────────────────────────────────────────────────────────────
# BACKTESTER
# ─────────────────────────────────────────────────────────────────────────────
class Backtester:
    """
    Replays the ORB strategy over multiple historical trading days.
    State resets every morning. Runs two configs simultaneously (current / tightened).
    """

    TIGHTENED: dict = {
        **ALGO_CONFIG,
        # Stricter ORB range: avoids too-narrow (choppy) and too-wide (news) days
        "orb_min_range_pct":     0.40,   # up from 0.30
        "orb_max_range_pct":     1.20,   # down from 1.50
        # No entries after 13:30 — afternoon reversals are dangerous
        "no_new_trades_after":  "13:30", # down from 14:30
        # Lower per-trade risk — preserves kill-switch headroom for more trades
        "max_loss_per_trade":    750,     # down from 1000
        # Slightly higher target (rewards waiting for bigger moves)
        "orb_target_multiple":   2.5,    # up from 2.0
    }

    # Tightened params + 5 quality filters applied during replay
    FILTERED: dict = {
        **TIGHTENED,
        "no_new_trades_after": "13:00",   # tighter than TIGHTENED's 13:30
    }

    def __init__(self, days: int = 30):
        self.days = days

    def run(
        self,
        auto_select:     bool  = True,
        top_n:           int   = 8,
        min_score:       float = 0.0,
        min_floor:       int   = 4,
        log_to_playbook: bool  = True,
    ) -> tuple[list[DayResult], list[DayResult], list[DayResult]]:
        """
        Download data, optionally run the instrument screener to auto-select
        the best universe, then replay all three configs.

        auto_select=True  — screens CANDIDATE_POOL (27 stocks), picks top_n
        auto_select=False — uses ALGO_CONFIG["instruments"] (hardcoded list)

        min_score   — only select stocks scoring above this (default 0.0)
        min_floor   — always select at least this many stocks (default 4)
        """
        from algo.screener import CANDIDATE_POOL, screen_candidates, print_screening_report

        pool = CANDIDATE_POOL if auto_select else ALGO_CONFIG["instruments"]

        print(f"\n  {_B}Downloading 15-min data  ({self.days} trading days){_RST}")
        label = "candidate pool" if auto_select else "configured universe"
        print(f"  {_D}  {label}: {', '.join(pool)}{_RST}\n")

        # ── Download all pool stocks ──────────────────────────────────────────
        all_data: dict[str, dict[date, list[Candle]]] = {}
        for sym in pool:
            bars_by_day = self._download(sym)
            if bars_by_day:
                all_data[sym] = bars_by_day
                print(f"  {_D}  {sym:12s} {len(bars_by_day):3d} days{_RST}")
            else:
                print(f"  {_R}  {sym:12s} no data{_RST}")

        # ── Download NIFTY50 for relative-strength filter ─────────────────────
        print(f"  {_D}  {'NIFTY50':12s}", end="", flush=True)
        nifty_data = self._download_nifty()
        print(f" {len(nifty_data):3d} days{_RST}")

        # ── All available trading dates (used in full for screening) ──────────
        all_dates = sorted({d for v in all_data.values() for d in v})

        # ── Instrument screener ───────────────────────────────────────────────
        if auto_select:
            print(f"\n  {_B}Running instrument screener on {len([s for s in CANDIDATE_POOL if s in all_data])} stocks…{_RST}")
            symbols, scores = screen_candidates(
                self._replay_day, all_data, nifty_data,
                all_dates, self.FILTERED,
                top_n=top_n, min_score=min_score, min_floor=min_floor,
            )
            print_screening_report(scores, top_n=top_n, min_score=min_score)
        else:
            symbols = [s for s in pool if s in all_data]

        # ── Trading days for main 3-way comparison (last N days) ─────────────
        trading_days = all_dates[-self.days:] if len(all_dates) >= self.days else all_dates

        start_str = trading_days[0].strftime("%d %b")
        end_str   = trading_days[-1].strftime("%d %b %Y")
        print(f"  {_D}  {len(trading_days)} trading days: {start_str} → {end_str}{_RST}\n")

        # ── Replay all three configs with selected symbols ────────────────────
        current_results:   list[DayResult] = []
        tightened_results: list[DayResult] = []
        filtered_results:  list[DayResult] = []

        from algo.regime import RegimeClassifier

        _clf  = RegimeClassifier()
        _env  = NoTradeEnvironment()
        _nifty_dates = sorted(nifty_data.keys())

        for d in trading_days:
            # ── Classify regime from first 3 NIFTY 15-min candles ────────────────
            nifty_today   = nifty_data.get(d, [])
            regime_result = (_clf.classify(nifty_today)
                             if len(nifty_today) >= 5 else None)
            regime_str    = regime_result.regime.value if regime_result else "UNKNOWN"

            # ── Environment gate (FILTERED only) ───────────────────────────
            d_idx = _nifty_dates.index(d) if d in _nifty_dates else -1
            prev_nifty_day   = _nifty_dates[d_idx - 1] if d_idx > 0 else None
            nifty_prev_close = (nifty_data[prev_nifty_day][-1].close
                                if prev_nifty_day else 0.0)
            nifty_open_px  = nifty_today[0].open       if nifty_today else 0.0
            nifty_orb_pct  = nifty_today[0].range_pct() if nifty_today else 0.0
            env_check = _env.check_day(
                d, nifty_orb_pct, nifty_prev_close, nifty_open_px,
            )

            # ── Replay all three configs ─────────────────────────────────────
            r_cur = self._replay_day(d, all_data, symbols, ALGO_CONFIG)
            r_tig = self._replay_day(d, all_data, symbols, self.TIGHTENED)
            r_cur.regime = r_tig.regime = regime_str

            if not env_check.passed:
                r_flt = DayResult(date=d, env_blocked=True, regime=regime_str)
                print(f"  {_D}  {d.strftime('%d %b')} ENV SKIP: {env_check.reason}{_RST}")
            elif regime_str == "CHOP":
                # CHOP days: coin-flip breakouts, skip FILTERED to avoid fees
                r_flt = DayResult(date=d, env_blocked=True, regime=regime_str)
                print(f"  {_D}  {d.strftime('%d %b')} CHOP SKIP: {regime_result.reason}{_RST}")
            else:
                r_flt = self._replay_day(
                    d, all_data, symbols, self.FILTERED,
                    nifty_data=nifty_data, use_filters=True,
                    regime_result=regime_result,
                )
                r_flt.regime = regime_str

            current_results.append(r_cur)
            tightened_results.append(r_tig)
            filtered_results.append(r_flt)

        if log_to_playbook:
            _log_filtered_to_playbook(filtered_results)
        return current_results, tightened_results, filtered_results

    # ── Data download ──────────────────────────────────────────────────────

    def _download(self, symbol: str) -> dict[date, list[Candle]]:
        """Download 15-min bars from yfinance, grouped by trading day.

        yfinance supports at most 60 calendar days per request for 15-min data.
        When ``self.days > 42`` (≈ the 59-calendar-day window), we attempt a
        second older 55-day window and merge the results; Yahoo Finance may or
        may not serve the older window depending on their retention policy.
        """
        try:
            import contextlib, io, yfinance as yf
            import pandas as pd
            from datetime import datetime as dt

            raw_frames: list = []

            # Always fetch the most recent window
            _sink = io.StringIO()
            with contextlib.redirect_stdout(_sink), \
                 contextlib.redirect_stderr(_sink):
                df_recent = yf.download(
                    f"{symbol}.NS",
                    period="59d",
                    interval="15m",
                    auto_adjust=True,
                    progress=False,
                )
            if not df_recent.empty:
                raw_frames.append(df_recent)

            # For >42 trading-day requests attempt an older window too
            if self.days > 42:
                try:
                    now      = dt.now()
                    end_old  = (now - timedelta(days=59)).strftime("%Y-%m-%d")
                    start_old = (now - timedelta(days=114)).strftime("%Y-%m-%d")
                    # Suppress yfinance's "not available" noise — this window will
                    # fail silently if Yahoo's 60-day 15-min retention doesn't cover it
                    _sink2 = io.StringIO()
                    with contextlib.redirect_stdout(_sink2), \
                         contextlib.redirect_stderr(_sink2):
                        df_old = yf.download(
                            f"{symbol}.NS",
                            start=start_old, end=end_old,
                            interval="15m",
                            auto_adjust=True,
                            progress=False,
                        )
                    if not df_old.empty:
                        raw_frames.append(df_old)
                except Exception:
                    pass  # older window not available — use what we have

            if not raw_frames:
                return {}

            df = pd.concat(raw_frames).sort_index()
            df = df[~df.index.duplicated(keep="first")]
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            by_day: dict[date, list[Candle]] = {}
            for idx, row in df.iterrows():
                ts = idx.to_pydatetime()
                if hasattr(ts, "tzinfo") and ts.tzinfo:
                    ts = ts.replace(tzinfo=None)
                d  = ts.date()
                if not (time(9, 15) <= ts.time() <= time(15, 20)):
                    continue
                if d not in by_day:
                    by_day[d] = []
                by_day[d].append(Candle(
                    timestamp=ts,
                    open=float(row["Open"]),   high=float(row["High"]),
                    low=float(row["Low"]),     close=float(row["Close"]),
                    volume=int(row.get("Volume", 0)),
                ))

            return {d: c for d, c in by_day.items() if len(c) >= 10}
        except Exception as e:
            print(f"  {_R}  [{symbol}] download error: {e}{_RST}")
            return {}

    def _download_nifty(self) -> dict[date, list[Candle]]:
        """Download NIFTY50 15-min bars for the relative-strength filter.
        Uses the same multi-period strategy as ``_download``."""
        try:
            import contextlib, io, yfinance as yf
            import pandas as pd
            from datetime import datetime as dt

            raw_frames: list = []

            _sink = io.StringIO()
            with contextlib.redirect_stdout(_sink), \
                 contextlib.redirect_stderr(_sink):
                df_recent = yf.download("^NSEI", period="59d", interval="15m",
                                        auto_adjust=True, progress=False)
            if not df_recent.empty:
                raw_frames.append(df_recent)

            if self.days > 42:
                try:
                    now       = dt.now()
                    end_old   = (now - timedelta(days=59)).strftime("%Y-%m-%d")
                    start_old = (now - timedelta(days=114)).strftime("%Y-%m-%d")
                    _sink2 = io.StringIO()
                    with contextlib.redirect_stdout(_sink2), \
                         contextlib.redirect_stderr(_sink2):
                        df_old = yf.download(
                            "^NSEI",
                            start=start_old, end=end_old,
                            interval="15m",
                            auto_adjust=True,
                            progress=False,
                        )
                    if not df_old.empty:
                        raw_frames.append(df_old)
                except Exception:
                    pass

            if not raw_frames:
                return {}

            df = pd.concat(raw_frames).sort_index()
            df = df[~df.index.duplicated(keep="first")]
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            by_day: dict[date, list[Candle]] = {}
            for idx, row in df.iterrows():
                ts = idx.to_pydatetime()
                if hasattr(ts, "tzinfo") and ts.tzinfo:
                    ts = ts.replace(tzinfo=None)
                d = ts.date()
                if not (time(9, 15) <= ts.time() <= time(15, 20)):
                    continue
                if d not in by_day:
                    by_day[d] = []
                by_day[d].append(Candle(
                    timestamp=ts,
                    open=float(row["Open"]),  high=float(row["High"]),
                    low=float(row["Low"]),    close=float(row["Close"]),
                    volume=0,
                ))
            return {d: c for d, c in by_day.items() if len(c) >= 10}
        except Exception:
            return {}

    # ── Day replay ──────────────────────────────────────────────────────────

    def _replay_day(self, trade_date: date, all_data: dict,
                    symbols: list[str], cfg: dict,
                    nifty_data: dict = None,
                    use_filters: bool = False,
                    regime_result=None) -> DayResult:
        dr = DayResult(date=trade_date)

        # ── Regime-adaptive exit multipliers ─────────────────────────────────
        _t1_mult    = getattr(regime_result, 't1_mult',    1.0)
        _t2_mult    = getattr(regime_result, 't2_mult',    cfg["orb_target_multiple"])
        _runner     = getattr(regime_result, 'has_runner', True)
        _regime_str = (regime_result.regime.value
                       if regime_result is not None else "UNKNOWN")

        no_after   = time(*map(int, cfg["no_new_trades_after"].split(":")))
        sq_time    = time(*map(int, cfg["square_off_time"].split(":")))
        max_risk   = cfg["max_loss_per_trade"]
        max_daily  = cfg["capital"] * cfg["max_daily_loss_pct"] / 100
        max_conc   = cfg["max_concurrent_positions"]
        max_trades = cfg["max_trades_per_day"]

        day_gross   = 0.0
        day_killed  = False
        open_count  = 0
        trade_count = 0

        # ── Instantiate filters once per day ─────────────────────────────────
        if use_filters:
            _quality     = BreakoutQualityFilter()
            _sweep       = LiquiditySweepDetector()
            _rs          = RelativeStrengthFilter()
            _time_win    = TimeWindowFilter(start="09:45", end="13:00")
            _gap         = GapFilter(max_gap_pct=1.5)
            _pdh         = PDHProximityFilter()
            _vol_confirm = VolumeConfirmationFilter()   # breakout-candle RVOL gate
            _orb_vol     = OrbVolumeGate()              # opening-candle volume gate
            _nifty_day   = (nifty_data or {}).get(trade_date, [])
        else:
            _quality = _sweep = _rs = _time_win = _gap = _pdh = None
            _vol_confirm = _orb_vol = _nifty_day = None

        for symbol in symbols:
            candles = all_data.get(symbol, {}).get(trade_date, [])
            if len(candles) < 2:
                dr.skipped += 1
                continue

            # ── Opening range ───────────────────────────────────────────
            orb       = candles[0]
            rng_pct   = orb.range_pct()

            if not (cfg["orb_min_range_pct"] <= rng_pct <= cfg["orb_max_range_pct"]):
                dr.skipped += 1
                continue

            if day_killed or trade_count >= max_trades:
                dr.rejected += 1
                continue

            # ── Gap filter + PDH/PDL (per-symbol) ───────────────────────────
            pdh = pdl = 0.0
            prior_sym_candles: list = []   # list[list[Candle]], oldest first
            atr14: float = 0.0
            if use_filters:
                sym_days = sorted(d for d in all_data.get(symbol, {}) if d < trade_date)
                prior_sym_candles = [all_data[symbol][d] for d in sym_days]
                if sym_days:
                    prior_day  = all_data[symbol][sym_days[-1]]
                    prev_close = prior_day[-1].close
                    pdh = max(cd.high for cd in prior_day)
                    pdl = min(cd.low  for cd in prior_day)
                    atr14 = _compute_atr(all_data.get(symbol, {}), trade_date)
                else:
                    prev_close = 0.0
                if not _gap.check(orb.open, prev_close).passed:
                    dr.filter_blocked += 1
                    continue
                # ── ORB volume gate (opening candle must have institutional participation)
                if not _orb_vol.check(orb.volume, prior_sym_candles).passed:
                    dr.filter_blocked += 1
                    continue

            buffer       = orb.high * (cfg["orb_buffer_pct"] / 100)
            buy_trig     = orb.high + buffer
            sell_trig    = orb.low  - buffer
            rng_width    = orb.high - orb.low
            tgt_width    = rng_width * cfg["orb_target_multiple"]

            # ── Tiered exit state (reset per symbol) ────────────────────────
            t1_hit    = False
            t1_gross  = 0.0
            t1_fees   = 0.0
            t1_qty    = 0
            t2_qty    = 0
            t1_target = 0.0
            t2_target = 0.0
            trade: Optional[BacktestTrade] = None

            for cidx, c in enumerate(candles[1:], start=1):
                ts = c.timestamp

                # ── Entry ──────────────────────────────────────────────
                if trade is None:
                    if ts.time() >= no_after:
                        break
                    if open_count >= max_conc or trade_count >= max_trades:
                        break

                    if c.close > buy_trig:
                        # 0.3 % fill slippage on the trigger price (no look-ahead)
                        entry_px = buy_trig * 1.003
                        if entry_px > c.high:   # price never reached fill level intrabar
                            continue
                        # ATR-adaptive stop for FILTERED; ORB-fraction otherwise
                        if use_filters and atr14 > 0:
                            stop_px = min(orb.low - rng_width * 0.05,
                                         entry_px - atr14 * 1.5)
                        else:
                            stop_px = orb.low - rng_width * 0.25
                        risk_ps = entry_px - stop_px
                        if risk_ps <= 0:
                            continue
                        qty = int(max_risk / risk_ps)
                        if qty <= 0:
                            continue
                        t1_target = entry_px + rng_width * _t1_mult
                        t2_target = entry_px + rng_width * _t2_mult
                        t1_qty    = qty if not _runner else max(1, qty // 2)
                        t2_qty    = 0   if not _runner else qty - t1_qty
                        tgt_px    = t2_target if _runner else t1_target
                        # ── Quality gate (LONG) ─────────────────────────
                        if use_filters:
                            nifty_close = (_nifty_day[min(cidx, len(_nifty_day)-1)].close
                                           if _nifty_day else 0.0)
                            nifty_open  = _nifty_day[0].open if _nifty_day else 0.0
                            gate = run_gate([
                                _time_win.check(ts.time()),
                                _quality.check(c, "LONG", candles[:cidx]),
                                _sweep.check(c, "LONG"),
                                _rs.check(candles[0].open, c.close,
                                          nifty_open, nifty_close, "LONG"),
                                (_pdh.check(entry_px, pdh, pdl, "LONG")
                                 if pdh > 0 else FilterResult(True, "PDH", "no data")),
                                _vol_confirm.check(c, cidx, prior_sym_candles),
                            ])
                            if not gate.all_passed:
                                dr.filter_blocked += 1
                                continue
                        trade = self._make_trade(trade_date, symbol, "LONG",
                                                 ts, entry_px, stop_px, tgt_px,
                                                 qty, rng_pct, _regime_str,
                                                 initial_stop_px=stop_px)
                        open_count  += 1
                        trade_count += 1

                    elif c.close < sell_trig:
                        # 0.3 % fill slippage on the trigger price (no look-ahead)
                        entry_px = sell_trig * 0.997
                        if entry_px < c.low:   # price never reached fill level intrabar
                            continue
                        # ATR-adaptive stop for FILTERED; ORB-fraction otherwise
                        if use_filters and atr14 > 0:
                            stop_px = max(orb.high + rng_width * 0.05,
                                         entry_px + atr14 * 1.5)
                        else:
                            stop_px = orb.high + rng_width * 0.25
                        risk_ps   = stop_px - entry_px
                        if risk_ps <= 0:
                            continue
                        qty = int(max_risk / risk_ps)
                        if qty <= 0:
                            continue
                        t1_target = entry_px - rng_width * _t1_mult
                        t2_target = entry_px - rng_width * _t2_mult
                        t1_qty    = qty if not _runner else max(1, qty // 2)
                        t2_qty    = 0   if not _runner else qty - t1_qty
                        tgt_px    = t2_target if _runner else t1_target
                        # ── Quality gate (SHORT) ────────────────────────
                        if use_filters:
                            nifty_close = (_nifty_day[min(cidx, len(_nifty_day)-1)].close
                                           if _nifty_day else 0.0)
                            nifty_open  = _nifty_day[0].open if _nifty_day else 0.0
                            gate = run_gate([
                                _time_win.check(ts.time()),
                                _quality.check(c, "SHORT", candles[:cidx]),
                                _sweep.check(c, "SHORT"),
                                _rs.check(candles[0].open, c.close,
                                          nifty_open, nifty_close, "SHORT"),
                                (_pdh.check(entry_px, pdh, pdl, "SHORT")
                                 if pdh > 0 else FilterResult(True, "PDH", "no data")),
                                _vol_confirm.check(c, cidx, prior_sym_candles),
                            ])
                            if not gate.all_passed:
                                dr.filter_blocked += 1
                                continue
                        trade = self._make_trade(trade_date, symbol, "SHORT",
                                                 ts, entry_px, stop_px, tgt_px,
                                                 qty, rng_pct, _regime_str,
                                                 initial_stop_px=stop_px)
                        open_count  += 1
                        trade_count += 1

                # ── Exit check (tiered: T1=1×range→BE stop, T2=full target) ──
                elif not trade.exit_reason:
                    if trade.direction == "LONG":
                        if not t1_hit:
                            if c.low <= trade.stop_px:
                                self._close(trade, trade.stop_px, ts, "SL")
                            elif c.high >= t1_target:
                                if t2_qty == 0:  # CHOP mode — no runner, close all at T1
                                    g = (t1_target - trade.entry_px) * trade.qty
                                    f = FeeModel.calculate(trade.entry_px, t1_target, trade.qty)
                                    trade.gross_pnl   = g
                                    trade.fees        = f
                                    trade.net_pnl     = g - f
                                    trade.exit_time   = ts
                                    trade.exit_px     = t1_target
                                    trade.exit_reason = "T1"
                                else:
                                    t1_hit   = True
                                    t1_gross = (t1_target - trade.entry_px) * t1_qty
                                    t1_fees  = FeeModel.calculate(trade.entry_px, t1_target, t1_qty)
                                    trade.stop_px = trade.entry_px   # breakeven stop
                                    trade.t1_hit  = True
                                    trade.t1_px   = t1_target
                                    if c.high >= t2_target:  # both targets in one candle
                                        t2_g = (t2_target - trade.entry_px) * t2_qty
                                        t2_f = FeeModel.calculate(trade.entry_px, t2_target, t2_qty)
                                        trade.gross_pnl   = t1_gross + t2_g
                                        trade.fees        = t1_fees  + t2_f
                                        trade.net_pnl     = trade.gross_pnl - trade.fees
                                        trade.exit_time   = ts
                                        trade.exit_px     = t2_target
                                        trade.exit_reason = "T1+T2"
                            elif ts.time() >= sq_time:
                                self._close(trade, c.close, ts, "EOD")
                        else:  # T1 already hit — watching T2 and BE stop
                            if c.low <= trade.stop_px:
                                t2_f = FeeModel.calculate(trade.entry_px, trade.entry_px, t2_qty)
                                trade.gross_pnl   = t1_gross
                                trade.fees        = t1_fees + t2_f
                                trade.net_pnl     = trade.gross_pnl - trade.fees
                                trade.exit_time   = ts
                                trade.exit_px     = trade.entry_px
                                trade.exit_reason = "T1+BE"
                            elif c.high >= t2_target:
                                t2_g = (t2_target - trade.entry_px) * t2_qty
                                t2_f = FeeModel.calculate(trade.entry_px, t2_target, t2_qty)
                                trade.gross_pnl   = t1_gross + t2_g
                                trade.fees        = t1_fees  + t2_f
                                trade.net_pnl     = trade.gross_pnl - trade.fees
                                trade.exit_time   = ts
                                trade.exit_px     = t2_target
                                trade.exit_reason = "T1+T2"
                            elif ts.time() >= sq_time:
                                t2_g = (c.close - trade.entry_px) * t2_qty
                                t2_f = FeeModel.calculate(trade.entry_px, c.close, t2_qty)
                                trade.gross_pnl   = t1_gross + t2_g
                                trade.fees        = t1_fees  + t2_f
                                trade.net_pnl     = trade.gross_pnl - trade.fees
                                trade.exit_time   = ts
                                trade.exit_px     = c.close
                                trade.exit_reason = "T1+EOD"
                    else:  # SHORT
                        if not t1_hit:
                            if c.high >= trade.stop_px:
                                self._close(trade, trade.stop_px, ts, "SL")
                            elif c.low <= t1_target:
                                if t2_qty == 0:  # CHOP mode — no runner, close all at T1
                                    g = (trade.entry_px - t1_target) * trade.qty
                                    f = FeeModel.calculate(trade.entry_px, t1_target, trade.qty)
                                    trade.gross_pnl   = g
                                    trade.fees        = f
                                    trade.net_pnl     = g - f
                                    trade.exit_time   = ts
                                    trade.exit_px     = t1_target
                                    trade.exit_reason = "T1"
                                else:
                                    t1_hit   = True
                                    t1_gross = (trade.entry_px - t1_target) * t1_qty
                                    t1_fees  = FeeModel.calculate(trade.entry_px, t1_target, t1_qty)
                                    trade.stop_px = trade.entry_px
                                    trade.t1_hit  = True
                                    trade.t1_px   = t1_target
                                    if c.low <= t2_target:
                                        t2_g = (trade.entry_px - t2_target) * t2_qty
                                        t2_f = FeeModel.calculate(trade.entry_px, t2_target, t2_qty)
                                        trade.gross_pnl   = t1_gross + t2_g
                                        trade.fees        = t1_fees  + t2_f
                                        trade.net_pnl     = trade.gross_pnl - trade.fees
                                        trade.exit_time   = ts
                                        trade.exit_px     = t2_target
                                        trade.exit_reason = "T1+T2"
                            elif ts.time() >= sq_time:
                                self._close(trade, c.close, ts, "EOD")
                        else:
                            if c.high >= trade.stop_px:
                                t2_f = FeeModel.calculate(trade.entry_px, trade.entry_px, t2_qty)
                                trade.gross_pnl   = t1_gross
                                trade.fees        = t1_fees + t2_f
                                trade.net_pnl     = trade.gross_pnl - trade.fees
                                trade.exit_time   = ts
                                trade.exit_px     = trade.entry_px
                                trade.exit_reason = "T1+BE"
                            elif c.low <= t2_target:
                                t2_g = (trade.entry_px - t2_target) * t2_qty
                                t2_f = FeeModel.calculate(trade.entry_px, t2_target, t2_qty)
                                trade.gross_pnl   = t1_gross + t2_g
                                trade.fees        = t1_fees  + t2_f
                                trade.net_pnl     = trade.gross_pnl - trade.fees
                                trade.exit_time   = ts
                                trade.exit_px     = t2_target
                                trade.exit_reason = "T1+T2"
                            elif ts.time() >= sq_time:
                                t2_g = (trade.entry_px - c.close) * t2_qty
                                t2_f = FeeModel.calculate(trade.entry_px, c.close, t2_qty)
                                trade.gross_pnl   = t1_gross + t2_g
                                trade.fees        = t1_fees  + t2_f
                                trade.net_pnl     = trade.gross_pnl - trade.fees
                                trade.exit_time   = ts
                                trade.exit_px     = c.close
                                trade.exit_reason = "T1+EOD"

            # EOD square-off if still open
            if trade and not trade.exit_reason:
                last = candles[-1]
                if t1_hit:
                    t2_g = ((last.close - trade.entry_px) * t2_qty
                            if trade.direction == "LONG"
                            else (trade.entry_px - last.close) * t2_qty)
                    t2_f = FeeModel.calculate(trade.entry_px, last.close, t2_qty)
                    trade.gross_pnl   = t1_gross + t2_g
                    trade.fees        = t1_fees  + t2_f
                    trade.net_pnl     = trade.gross_pnl - trade.fees
                    trade.exit_time   = last.timestamp
                    trade.exit_px     = last.close
                    trade.exit_reason = "T1+EOD"
                else:
                    self._close(trade, last.close, last.timestamp, "EOD")

            if trade and trade.exit_reason:
                day_gross  += trade.gross_pnl
                open_count -= 1
                dr.trades.append(trade)

                # Daily kill switch (check after each trade exit)
                if day_gross <= -max_daily:
                    day_killed = True

        return dr

    # ── Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _make_trade(d, sym, direction, ts, entry_px, stop_px, tgt_px,
                    qty, rng_pct, regime: str = "UNKNOWN",
                    initial_stop_px: float = 0.0) -> BacktestTrade:
        isp = initial_stop_px if initial_stop_px != 0.0 else stop_px
        return BacktestTrade(
            trade_date=d, symbol=sym, direction=direction,
            entry_time=ts, entry_px=entry_px, stop_px=stop_px,
            target_px=tgt_px, exit_time=ts, exit_px=entry_px,
            qty=qty, gross_pnl=0.0, fees=0.0, net_pnl=0.0,
            exit_reason="", range_pct=rng_pct, regime=regime,
            initial_stop_px=isp,
        )

    @staticmethod
    def _close(trade: BacktestTrade, exit_px: float,
               exit_time: datetime, reason: str) -> None:
        trade.exit_px     = exit_px
        trade.exit_time   = exit_time
        trade.exit_reason = reason
        if trade.direction == "LONG":
            trade.gross_pnl = (exit_px - trade.entry_px) * trade.qty
        else:
            trade.gross_pnl = (trade.entry_px - exit_px) * trade.qty
        trade.fees    = FeeModel.calculate(trade.entry_px, exit_px, trade.qty)
        trade.net_pnl = trade.gross_pnl - trade.fees


# ─────────────────────────────────────────────────────────────────────────────
# REPORT PRINTING
# ─────────────────────────────────────────────────────────────────────────────

def _max_drawdown(cumulative_pnl: list[float]) -> float:
    """Max peak-to-trough drawdown from a series of cumulative P&L values."""
    peak = 0.0
    max_dd = 0.0
    for v in cumulative_pnl:
        if v > peak:
            peak = v
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _sharpe(daily_pnl: list[float]) -> float:
    """Annualised Sharpe ratio (simplified, 0% risk-free rate, 252 trading days)."""
    if len(daily_pnl) < 3:
        return 0.0
    n   = len(daily_pnl)
    avg = sum(daily_pnl) / n
    var = sum((x - avg) ** 2 for x in daily_pnl) / n
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return round((avg / std) * math.sqrt(252), 2)


def _pct(n, d) -> str:
    return f"{n/d*100:.1f}%" if d else "—"


def print_report(current: list[DayResult], tightened: list[DayResult],
                 filtered: list[DayResult] = None) -> None:
    """Print the full backtest report comparing all three configs."""
    _print_day_table(current, filtered or tightened,
                     label_b="FILTERED" if filtered else "TIGHTENED")
    _print_summary("CURRENT CONFIG", current, ALGO_CONFIG)
    _print_summary("TIGHTENED CONFIG", tightened, Backtester.TIGHTENED)
    if filtered:
        _print_summary("FILTERED CONFIG  (quality+volume+RS+time+gap)",
                       filtered, Backtester.FILTERED)
    _print_comparison(current, tightened, filtered)
    _print_per_symbol(filtered or current)
    _print_fee_analysis(filtered or current)
    _print_recommendation(current, tightened, filtered)


def _print_day_table(current: list[DayResult], second: list[DayResult],
                     label_b: str = "TIGHTENED") -> None:
    """Day-by-day P&L table showing current vs second config."""
    print(f"\n  {_B}{'═'*104}{_RST}")
    print(f"  {_B}  DAY-BY-DAY RESULTS  (CURRENT  vs  {label_b}){_RST}")
    print(f"  {_B}{'═'*104}{_RST}")
    print(f"  {'DATE':12s}  {'DOW':3s}  "
          f"{'─── CURRENT ───':>30s}  "
          f"{'─── ' + label_b + ' ──':>30s}  {'REGIME':10s}")
    print(f"  {'':12s}  {'':3s}  "
          f"{'TRD':>3s} {'W':>2s} {'L':>2s} {'NET P&L':>10s} {'CUMUL':>10s}  "
          f"{'TRD':>3s} {'W':>2s} {'L':>2s} {'NET P&L':>10s} {'CUMUL':>10s} {'FLT':>4s}")
    print(f"  {'─'*104}")
    tightened = second

    c_cumul = 0.0
    t_cumul = 0.0

    for cr, tr in zip(current, tightened):
        c_cumul += cr.net_pnl
        t_cumul += tr.net_pnl

        c_clr = _G if cr.net_pnl >= 0 else _R
        t_clr = _G if tr.net_pnl >= 0 else _R
        c_cu  = _G if c_cumul >= 0 else _R
        t_cu  = _G if t_cumul >= 0 else _R

        dow        = cr.date.strftime("%a")
        flt        = getattr(tr, "filter_blocked", 0)
        regime_str = getattr(tr, "regime", "UNKNOWN") or "UNKNOWN"
        env_blk    = getattr(tr, "env_blocked", False)
        regime_clr = (_R if env_blk
                      else _Y if regime_str == "CHOP"
                      else _G if "TREND" in regime_str
                      else _D)
        print(
            f"  {cr.date.strftime('%d %b %Y'):12s}  {dow:3s}  "
            f"{len(cr.trades):>3d} {cr.wins:>2d} {cr.losses:>2d} "
            f"{c_clr}{cr.net_pnl:>+10,.2f}{_RST} "
            f"{c_cu}{c_cumul:>+10,.2f}{_RST}  "
            f"{len(tr.trades):>3d} {tr.wins:>2d} {tr.losses:>2d} "
            f"{t_clr}{tr.net_pnl:>+10,.2f}{_RST} "
            f"{t_cu}{t_cumul:>+10,.2f}{_RST}"
            + (f" {_D}{flt:>3d}↓{_RST}" if flt else "     ")
            + f"  {regime_clr}{'ENV_SKIP' if env_blk else regime_str}{_RST}"
        )

    print(f"  {'─'*104}")


def _print_summary(label: str, results: list[DayResult], cfg: dict) -> None:
    """Summary stats for one config."""
    all_trades = [t for dr in results for t in dr.trades]
    if not all_trades:
        print(f"\n  {_R}  {label}: No trades found.{_RST}\n")
        return

    total_gross = sum(t.gross_pnl for t in all_trades)
    total_fees  = sum(t.fees      for t in all_trades)
    total_net   = sum(t.net_pnl   for t in all_trades)
    wins        = [t for t in all_trades if t.net_pnl > 0]
    losses      = [t for t in all_trades if t.net_pnl <= 0]
    sl_exits  = [t for t in all_trades if t.exit_reason == "SL"]
    tgt_exits = [t for t in all_trades if t.exit_reason in ("TARGET", "T1+T2")]
    t1_exits  = [t for t in all_trades if t.exit_reason == "T1"]    # CHOP full exit
    eod_exits = [t for t in all_trades if t.exit_reason in ("EOD", "T1+EOD")]
    be_exits  = [t for t in all_trades if t.exit_reason == "T1+BE"]
    profit_days = [dr for dr in results if dr.net_pnl > 0]
    loss_days   = [dr for dr in results if dr.net_pnl < 0]
    flat_days   = [dr for dr in results if dr.net_pnl == 0]

    avg_win     = sum(t.net_pnl for t in wins)   / len(wins)   if wins   else 0
    avg_loss    = sum(t.net_pnl for t in losses) / len(losses) if losses else 0
    pf          = abs(sum(t.net_pnl for t in wins) / sum(t.net_pnl for t in losses)) if losses else float("inf")

    daily_pnl   = [dr.net_pnl for dr in results]
    cumul       = []
    running     = 0.0
    for v in daily_pnl:
        running += v
        cumul.append(running)
    max_dd  = _max_drawdown(cumul)
    sharpe  = _sharpe(daily_pnl)

    # Max consecutive losses
    max_consec = consec = 0
    for dr in results:
        if dr.net_pnl < 0:
            consec += 1
            max_consec = max(max_consec, consec)
        else:
            consec = 0

    clr = _G if total_net >= 0 else _R

    print(f"\n  {_B}{'─'*56}{_RST}")
    print(f"  {_B}  {label}{_RST}")
    print(f"  {_B}{'─'*56}{_RST}")
    print(f"  Config:  orb_range={cfg['orb_min_range_pct']}–{cfg['orb_max_range_pct']}%  "
          f"target={cfg['orb_target_multiple']}×  "
          f"no_new_after={cfg['no_new_trades_after']}  "
          f"risk/trade=₹{cfg['max_loss_per_trade']:,}")
    print()
    print(f"  Total trades:      {len(all_trades)}  "
          f"(SL: {len(sl_exits)}  T1+T2: {len(tgt_exits)}  "
          f"T1: {len(t1_exits)}  T1+BE: {len(be_exits)}  EOD: {len(eod_exits)})")
    print(f"  Win rate:          {_pct(len(wins), len(all_trades))}  "
          f"({len(wins)}/{len(all_trades)} trades)")
    print(f"  Profit days:       {_pct(len(profit_days), len(results))}  "
          f"({len(profit_days)}/{len(results)} days)")
    print(f"  Loss days:         {_pct(len(loss_days),  len(results))}  "
          f"({len(loss_days)}/{len(results)} days)")
    print(f"  Flat days:         {len(flat_days)}  (no trades / all skipped)")
    print()
    print(f"  Gross P&L:         ₹{total_gross:>+10,.2f}")
    print(f"  Total fees:        ₹{total_fees:>+10,.2f}  "
          f"(avg ₹{total_fees/len(all_trades):.0f}/trade)")
    print(f"  Net P&L:           {clr}₹{total_net:>+10,.2f}{_RST}  "
          f"({total_net/cfg['capital']*100:+.2f}% on ₹{cfg['capital']:,} capital)")
    print()
    print(f"  Avg trade (net):   ₹{total_net/len(all_trades):>+8,.2f}")
    print(f"  Avg win (net):     ₹{avg_win:>+8,.2f}")
    print(f"  Avg loss (net):    ₹{avg_loss:>+8,.2f}")
    print(f"  Profit factor:     {pf:.2f}  "
          f"(>1.5 is viable, >2.0 is good)")
    print()
    print(f"  Max drawdown:      ₹{max_dd:>8,.2f}  "
          f"({max_dd/cfg['capital']*100:.1f}% of capital)")
    print(f"  Max consec losses: {max_consec} days in a row")
    print(f"  Daily Sharpe:      {sharpe:>+5.2f}  (>0.5 is viable)")
    print(f"  {'─'*56}")


def _print_comparison(current: list[DayResult], tightened: list[DayResult],
                      filtered: list[DayResult] = None) -> None:
    """Head-to-head comparison table (2 or 3 configs)."""
    def _stats(results):
        trades = [t for dr in results for t in dr.trades]
        net    = sum(t.net_pnl for t in trades)
        wins   = sum(1 for t in trades if t.net_pnl > 0)
        fees   = sum(t.fees for t in trades)
        daily  = [dr.net_pnl for dr in results]
        fblk   = sum(getattr(dr, "filter_blocked", 0) for dr in results)
        cumul  = []
        r = 0.0
        for v in daily:
            r += v
            cumul.append(r)
        return {
            "trades":        len(trades),
            "filter_blocked": fblk,
            "win_rate":       wins / len(trades) * 100 if trades else 0,
            "net":        net,
            "fees":       fees,
            "sharpe":     _sharpe(daily),
            "max_dd":     _max_drawdown(cumul),
            "profit_days": sum(1 for dr in results if dr.net_pnl > 0),
        }

    c = _stats(current)
    t = _stats(tightened)
    f = _stats(filtered) if filtered else None

    def _clr(cv, fv, higher=True):
        if fv is None: return _RST
        return _G if (fv > cv if higher else fv < cv) else (_R if (fv < cv if higher else fv > cv) else _RST)

    col = 72 if f else 56
    print(f"\n  {_B}{'─'*col}{_RST}")
    print(f"  {_B}  HEAD-TO-HEAD COMPARISON{_RST}")
    print(f"  {'─'*col}")
    hdr = f"  {'Metric':28s}  {'CURRENT':>12s}  {'TIGHTENED':>12s}"
    if f: hdr += f"  {'FILTERED':>12s}"
    print(hdr)
    print(f"  {'─'*col}")

    rows = [
        ("Total trades",      c['trades'],         t['trades'],         f['trades']         if f else None, False),
        ("Setups filtered out",0,                  0,                   f['filter_blocked']  if f else None, False),
        ("Win rate %",         c['win_rate'],       t['win_rate'],       f['win_rate']        if f else None, True),
        ("Net P&L (₹)",        c['net'],            t['net'],            f['net']             if f else None, True),
        ("Total fees (₹)",     c['fees'],           t['fees'],           f['fees']            if f else None, False),
        ("Daily Sharpe",       c['sharpe'],         t['sharpe'],         f['sharpe']          if f else None, True),
        ("Max drawdown (₹)",   c['max_dd'],         t['max_dd'],         f['max_dd']          if f else None, False),
        ("Profit days",        c['profit_days'],    t['profit_days'],    f['profit_days']     if f else None, True),
    ]
    for label, cv, tv, fv, higher in rows:
        cv_s = f"{cv:>+,.0f}" if isinstance(cv, float) else str(cv)
        tv_s = f"{tv:>+,.0f}" if isinstance(tv, float) else str(tv)
        fv_s = (f"{fv:>+,.0f}" if isinstance(fv, float) else str(fv)) if fv is not None else ""
        clr  = _clr(cv, fv, higher)
        row  = f"  {label:28s}  {cv_s:>12s}  {tv_s:>12s}"
        if f: row += f"  {clr}{fv_s:>12s}{_RST}"
        print(row)
    print(f"  {'─'*col}")


def _print_per_symbol(results: list[DayResult]) -> None:
    """Per-stock breakdown for the given result set."""
    label = "FILTERED" if any(getattr(dr, "filter_blocked", 0) for dr in results) else "CURRENT"
    print(f"\n  {_B}  PER-STOCK BREAKDOWN ({label} CONFIG){_RST}")
    print(f"  {'─'*72}")
    print(f"  {'SYMBOL':12s}  {'TRD':>4s}  {'W':>3s}  {'L':>3s}  "
          f"{'WIN%':>6s}  {'NET P&L':>10s}  {'FEES':>8s}  {'AVG/TRD':>9s}")
    print(f"  {'─'*72}")

    all_trades = [t for dr in results for t in dr.trades]
    symbols    = sorted({t.symbol for t in all_trades}) or ALGO_CONFIG["instruments"]

    for sym in symbols:
        sym_trades = [t for t in all_trades if t.symbol == sym]
        if not sym_trades:
            print(f"  {sym:12s}  {'—':>4s}  {'—':>3s}  {'—':>3s}  "
                  f"{'—':>6s}  {'—':>10s}  {'—':>8s}  {'—':>9s}")
            continue
        wins   = sum(1 for t in sym_trades if t.net_pnl > 0)
        net    = sum(t.net_pnl for t in sym_trades)
        fees   = sum(t.fees    for t in sym_trades)
        avg    = net / len(sym_trades)
        clr    = _G if net >= 0 else _R
        print(f"  {sym:12s}  {len(sym_trades):>4d}  {wins:>3d}  "
              f"{len(sym_trades)-wins:>3d}  "
              f"{_pct(wins, len(sym_trades)):>6s}  "
              f"{clr}₹{net:>+9,.0f}{_RST}  "
              f"₹{fees:>7,.0f}  "
              f"{clr}₹{avg:>+8,.0f}{_RST}")

    total_net  = sum(t.net_pnl for t in all_trades)
    total_fees = sum(t.fees    for t in all_trades)
    total_wins = sum(1 for t in all_trades if t.net_pnl > 0)
    clr = _G if total_net >= 0 else _R
    print(f"  {'─'*72}")
    if all_trades:
        print(f"  {'TOTAL':12s}  {len(all_trades):>4d}  {total_wins:>3d}  "
              f"{len(all_trades)-total_wins:>3d}  "
              f"{_pct(total_wins, len(all_trades)):>6s}  "
              f"{clr}₹{total_net:>+9,.0f}{_RST}  "
              f"₹{total_fees:>7,.0f}  "
              f"{clr}₹{total_net/len(all_trades):>+8,.0f}{_RST}")
    print(f"  {'─'*72}")


def _print_fee_analysis(current: list[DayResult]) -> None:
    """Show how much fees eat into gross P&L."""
    all_trades  = [t for dr in current for t in dr.trades]
    if not all_trades:
        return
    total_gross = sum(t.gross_pnl for t in all_trades)
    total_fees  = sum(t.fees      for t in all_trades)
    total_net   = sum(t.net_pnl   for t in all_trades)

    avg_trade_val = sum(t.entry_px * t.qty for t in all_trades) / len(all_trades)
    avg_fees      = total_fees / len(all_trades)

    # Sample breakdown for one average trade
    avg_entry = sum(t.entry_px for t in all_trades) / len(all_trades)
    avg_qty   = sum(t.qty      for t in all_trades) / len(all_trades)
    sample_fees = FeeModel.breakdown(avg_entry, avg_entry, int(avg_qty))

    print(f"\n  {_B}  FEE DRAG ANALYSIS{_RST}")
    print(f"  {'─'*56}")
    print(f"  Gross P&L:        ₹{total_gross:>+10,.2f}")
    print(f"  Total fees:       ₹{total_fees:>+10,.2f}")
    print(f"  Net P&L:          ₹{total_net:>+10,.2f}")
    print(f"  Fee drag:         "
          f"{abs(total_fees/total_gross*100) if total_gross else 0:.1f}% of gross P&L")
    print(f"  Avg fee/trade:    ₹{avg_fees:.2f}")
    print(f"  Avg trade value:  ₹{avg_trade_val:,.0f}")
    print()
    print(f"  {_D}  Fee breakdown per avg trade (entry ≈ ₹{avg_entry:,.0f}, qty ≈ {int(avg_qty)}){_RST}")
    for k, v in sample_fees.items():
        print(f"    {k:20s}  ₹{v:.2f}")
    print(f"  {'─'*56}")


def _print_recommendation(current: list[DayResult], tightened: list[DayResult],
                           filtered: list[DayResult] = None) -> None:
    """Honest assessment and actionable recommendations."""
    c_trades  = [t for dr in current   for t in dr.trades]
    t_trades  = [t for dr in tightened for t in dr.trades]
    f_trades  = [t for dr in filtered  for t in dr.trades] if filtered else []
    c_net     = sum(t.net_pnl for t in c_trades)
    t_net     = sum(t.net_pnl for t in t_trades)
    f_net     = sum(t.net_pnl for t in f_trades) if f_trades else None
    c_winrate = sum(1 for t in c_trades if t.net_pnl > 0) / len(c_trades) * 100 if c_trades else 0
    t_winrate = sum(1 for t in t_trades if t.net_pnl > 0) / len(t_trades) * 100 if t_trades else 0
    f_winrate = sum(1 for t in f_trades if t.net_pnl > 0) / len(f_trades) * 100 if f_trades else 0
    f_blocked = sum(getattr(dr, "filter_blocked", 0) for dr in (filtered or []))
    n_days    = len(current)

    print(f"\n  {_B}{'═'*70}{_RST}")
    print(f"  {_B}  VERDICT & RECOMMENDATIONS{_RST}")
    print(f"  {_B}{'═'*70}{_RST}\n")

    # Verdict on current config
    if c_net > 0:
        verdict_clr = _G
        verdict_txt = "PROFITABLE — edge exists on this data"
    elif c_net > -5000:
        verdict_clr = _Y
        verdict_txt = "MARGINAL — small losses, could be noise"
    else:
        verdict_clr = _R
        verdict_txt = "LOSING — negative expectancy over this period"

    print(f"  Current config:   {verdict_clr}{_B}{verdict_txt}{_RST}")
    print(f"  Win rate:         {c_winrate:.1f}%  "
          f"(need >45% with 2:1 RR to break even after fees)")
    print(f"  Net P&L / {n_days} days: ₹{c_net:+,.0f}  "
          f"(₹{c_net/n_days:+,.0f}/day avg)")

    if t_net > c_net:
        print(f"\n  {_G}Tightened config improves results by ₹{t_net - c_net:+,.0f} "
              f"over {n_days} days{_RST}")
        print(f"  Win rate: {c_winrate:.1f}% → {t_winrate:.1f}%")
    else:
        print(f"\n  {_Y}Tightened config makes minimal difference "
              f"(₹{t_net - c_net:+,.0f} vs current){_RST}")

    if f_net is not None:
        delta = f_net - c_net
        clr   = _G if delta >= 0 else _R
        print(f"\n  {_B}Filtered config (5 quality gates applied):{_RST}")
        print(f"  Net P&L: ₹{c_net:+,.0f} → {clr}₹{f_net:+,.0f}{_RST}  "
              f"(delta: {clr}₹{delta:+,.0f}{_RST} over {n_days} days)")
        print(f"  Win rate: {c_winrate:.1f}% → {clr}{f_winrate:.1f}%{_RST}  "
              f"| Setups blocked by filters: {f_blocked}")
        print(f"  Trades: {len(c_trades)} → {len(f_trades)}  "
              f"({len(c_trades)-len(f_trades)} low-quality setups eliminated)")

    print(f"\n  {_B}What to do:{_RST}")
    print(f"  1. {_Y}Win rate {c_winrate:.0f}% is the core problem.{_RST} ORB on large caps tends "
          f"to\n     have high false breakout rate. Consider adding a VOLUME FILTER:\n"
          f"     only take trades if breakout candle volume > 1.5× avg of prior 5 candles.")
    print(f"  2. {_Y}TCS and ICICIBANK are high-price stocks{_RST} — small % moves = large ₹ risk.\n"
          f"     Replace with BAJFINANCE or LTIM for better risk-reward geometry.")
    print(f"  3. {_Y}The 2× target is rarely hit{_RST} before reversal. Run a TARGET=1.5× backtest\n"
          f"     to see if more trades exit at target rather than SL or EOD.")
    print(f"  4. {_Y}Fee drag is real{_RST} — ₹{sum(t.fees for t in c_trades)/len(c_trades):.0f}/trade avg.\n"
          f"     Every marginally profitable trade becomes a loss after fees. \n"
          f"     Only take setups with RR >= 1.8:1 minimum.")
    print(f"  5. {_Y}Paper trade for 2 weeks before risking real capital.{_RST}\n"
          f"     Verify fills, slippage, and your own execution discipline first.\n")
    print(f"  {_B}{'═'*70}{_RST}\n")


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────

def _log_filtered_to_playbook(results: list[DayResult]) -> None:
    """Log all filtered backtest trades to the SQLite playbook DB."""
    try:
        from analytics.playbook import Playbook, PlaybookTrade
        pb = Playbook()
        n = 0
        for dr in results:
            if dr.env_blocked:
                continue
            for t in dr.trades:
                pt = PlaybookTrade(
                    source="backtest",
                    trade_date=t.trade_date,
                    symbol=t.symbol,
                    direction=t.direction,
                    entry_price=t.entry_px,
                    exit_price=t.exit_px,
                    stop_price=t.initial_stop_px,   # use original stop, not BE stop
                    quantity=t.qty,
                    t1_price=t.t1_px,
                    t2_price=t.target_px,            # set T2 so R-multiple is meaningful
                    gross_pnl=t.gross_pnl,
                    fees=t.fees,
                    net_pnl=t.net_pnl,
                    exit_reason=t.exit_reason,
                    orb_range_pct=t.range_pct,
                    regime=t.regime,
                )
                row_id = pb.log_trade(pt)
                if row_id:
                    n += 1
        print(f"\n  {_D}  Playbook: {n} trades logged  →  Journal/playbook.db{_RST}")
    except Exception as exc:
        print(f"  {_D}  [playbook] warning: {exc}{_RST}")


# ─────────────────────────────────────────────────────────────────────────────
# ATR HELPER  (reconstructs daily OHLC from 15-min bars)
# ─────────────────────────────────────────────────────────────────────────────

def _compute_atr(symbol_data: dict, trade_date: date, period: int = 14) -> float:
    """
    Compute the 14-day Average True Range for *symbol_data* up to (but not
    including) *trade_date*, using daily OHLC reconstructed from 15-min candles.

    Returns 0.0 if there are fewer than 2 prior days of data.
    """
    sorted_days = sorted(d for d in symbol_data if d < trade_date)
    if len(sorted_days) < 2:
        return 0.0

    daily_tr: list[float] = []
    prev_close = 0.0
    for d in sorted_days[-(period + 1):]:
        candles = symbol_data.get(d, [])
        if not candles:
            continue
        day_high  = max(c.high  for c in candles)
        day_low   = min(c.low   for c in candles)
        day_close = candles[-1].close
        if prev_close > 0:
            tr = max(
                day_high - day_low,
                abs(day_high - prev_close),
                abs(day_low  - prev_close),
            )
            daily_tr.append(tr)
        prev_close = day_close

    if not daily_tr:
        return 0.0
    use = daily_tr[-period:]
    return sum(use) / len(use)


def run_backtest(
    days: int = 30, auto_select: bool = True,
    top_n: int = 8, min_score: float = 0.0, min_floor: int = 4,
    log_to_playbook: bool = True,
) -> None:
    """Entry point called from run.py."""
    print(f"\n  {_B}{'═'*70}{_RST}")
    print(f"  {_B}  ORB BACKTEST  —  Last {days} Trading Days{_RST}")
    print(f"  {_B}  Capital: ₹{ALGO_CONFIG['capital']:,}  |  Fee model: Zerodha MIS (NSE){_RST}")
    if auto_select:
        print(f"  {_B}  Universe: AUTO-SELECTED  (screener → score>0, min {min_floor} stocks, max {top_n}){_RST}")
    else:
        print(f"  {_B}  Universe: MANUAL  ({', '.join(ALGO_CONFIG['instruments'])}){_RST}")
    print(f"  {_B}  3-way comparison: Current / Tightened / Filtered{_RST}")
    print(f"  {_B}{'═'*70}{_RST}")

    bt = Backtester(days=days)
    current, tightened, filtered = bt.run(
        auto_select=auto_select, top_n=top_n,
        min_score=min_score, min_floor=min_floor,
        log_to_playbook=log_to_playbook,
    )
    print_report(current, tightened, filtered)


def run_screen(days: int = 30, top_n: int = 8, min_score: float = 0.0, min_floor: int = 4) -> None:
    """Standalone screener — show the ORB edge ranking without running the full backtest."""
    from algo.screener import CANDIDATE_POOL, screen_candidates, print_screening_report

    print(f"\n  {_B}{'═'*70}{_RST}")
    print(f"  {_B}  ORB INSTRUMENT SCREENER  —  {len(CANDIDATE_POOL)}-stock candidate pool{_RST}")
    print(f"  {_B}  Capital: ₹{ALGO_CONFIG['capital']:,}  |  Fee model: Zerodha MIS (NSE){_RST}")
    print(f"  {_B}{'═'*70}{_RST}")

    bt = Backtester(days=days)

    print(f"\n  {_B}Downloading {len(CANDIDATE_POOL)}-stock candidate pool…{_RST}\n")
    all_data: dict = {}
    for sym in CANDIDATE_POOL:
        bars = bt._download(sym)
        if bars:
            all_data[sym] = bars
            print(f"  {_D}  {sym:12s} {len(bars):3d} days{_RST}")
        else:
            print(f"  {_R}  {sym:12s} no data{_RST}")

    print(f"  {_D}  {'NIFTY50':12s}", end="", flush=True)
    nifty_data = bt._download_nifty()
    print(f" {len(nifty_data):3d} days{_RST}")

    all_dates = sorted({d for v in all_data.values() for d in v})

    selected, scores = screen_candidates(
        bt._replay_day, all_data, nifty_data,
        all_dates, Backtester.FILTERED,
        top_n=top_n, min_score=min_score, min_floor=min_floor,
    )
    print_screening_report(scores, top_n=top_n, min_score=min_score)


# ─────────────────────────────────────────────────────────────────────────────
# WALK-FORWARD VALIDATION
# ─────────────────────────────────────────────────────────────────────────────

def run_walkforward(days: int = 120) -> None:
    """
    Walk-forward validation for the FILTERED config.

    Splits ``days`` trading days into:
      • In-sample  (IS):  first 80% — the config was tuned on this window
      • Out-of-sample (OOS): last 20% — the honest, forward-looking test

    Interpretation guide:
      OOS PF ≥ 2.0  and  net P&L > 0  → edge is real, proceed to paper trading
      OOS PF 1.5–2.0                   → marginal; paper trade 3–4 more weeks
      OOS P&L < 0                      → edge is overfit; do NOT live-trade
    """
    from algo.screener import CANDIDATE_POOL, screen_candidates, print_screening_report

    print(f"\n  {_B}{'═'*70}{_RST}")
    print(f"  {_B}  WALK-FORWARD VALIDATION  —  {days} Trading Days{_RST}")
    print(f"  {_B}  Split: first 80% = in-sample  |  last 20% = out-of-sample{_RST}")
    print(f"  {_B}  Config: FILTERED (regime + env + quality gates){_RST}")
    print(f"  {_B}{'═'*70}{_RST}")

    bt = Backtester(days=days)

    # ── Download all data ────────────────────────────────────────────────────
    print(f"\n  {_B}Downloading data…{_RST}\n")
    all_data: dict = {}
    for sym in CANDIDATE_POOL:
        bars = bt._download(sym)
        if bars:
            all_data[sym] = bars
            print(f"  {_D}  {sym:12s} {len(bars):3d} days{_RST}")
        else:
            print(f"  {_R}  {sym:12s} no data{_RST}")
    print(f"  {_D}  {'NIFTY50':12s}", end="", flush=True)
    nifty_data = bt._download_nifty()
    print(f" {len(nifty_data):3d} days{_RST}")

    all_dates    = sorted({d for v in all_data.values() for d in v})
    trading_days = all_dates[-days:] if len(all_dates) >= days else all_dates

    split_idx = int(len(trading_days) * 0.80)
    is_days   = trading_days[:split_idx]
    oos_days  = trading_days[split_idx:]

    if not is_days or not oos_days:
        print(f"\n  {_R}  Not enough trading days for a walk-forward split.{_RST}\n")
        return

    print(f"\n  {_D}  In-sample:      "
          f"{is_days[0].strftime('%d %b %Y')} → {is_days[-1].strftime('%d %b %Y')}"
          f"  ({len(is_days)} days){_RST}")
    print(f"  {_D}  Out-of-sample:  "
          f"{oos_days[0].strftime('%d %b %Y')} → {oos_days[-1].strftime('%d %b %Y')}"
          f"  ({len(oos_days)} days){_RST}\n")

    # ── Screen universe on IS data only ──────────────────────────────────────
    print(f"  {_B}Running screener on in-sample data…{_RST}")
    symbols, scores = screen_candidates(
        bt._replay_day, all_data, nifty_data,
        is_days, Backtester.FILTERED,
        top_n=8, min_score=0.0, min_floor=4,
    )
    print_screening_report(scores, top_n=8, min_score=0.0)
    print(f"  {_D}  Universe locked on IS data → applied unchanged to OOS.{_RST}\n")

    # ── Replay both segments with identical FILTERED config ──────────────────
    from algo.regime import RegimeClassifier
    from algo.filters import NoTradeEnvironment
    _clf         = RegimeClassifier()
    _env         = NoTradeEnvironment()
    _nifty_dates = sorted(nifty_data.keys())

    def _replay_segment(days_list: list) -> list[DayResult]:
        results: list[DayResult] = []
        for d in days_list:
            nifty_today   = nifty_data.get(d, [])
            regime_result = _clf.classify(nifty_today) if len(nifty_today) >= 5 else None
            regime_str    = regime_result.regime.value if regime_result else "UNKNOWN"
            d_idx = _nifty_dates.index(d) if d in _nifty_dates else -1
            prev_day      = _nifty_dates[d_idx - 1] if d_idx > 0 else None
            nifty_prev_close = (nifty_data[prev_day][-1].close if prev_day else 0.0)
            nifty_open_px    = nifty_today[0].open       if nifty_today else 0.0
            nifty_orb_pct    = nifty_today[0].range_pct() if nifty_today else 0.0
            env_check = _env.check_day(d, nifty_orb_pct, nifty_prev_close, nifty_open_px)
            if not env_check.passed:
                results.append(DayResult(date=d, env_blocked=True, regime=regime_str))
            elif regime_str == "CHOP":
                results.append(DayResult(date=d, env_blocked=True, regime=regime_str))
            else:
                r = bt._replay_day(
                    d, all_data, symbols, Backtester.FILTERED,
                    nifty_data=nifty_data, use_filters=True,
                    regime_result=regime_result,
                )
                r.regime = regime_str
                results.append(r)
        return results

    print(f"  {_B}Replaying in-sample ({len(is_days)} days)…{_RST}")
    is_results  = _replay_segment(is_days)
    print(f"  {_B}Replaying out-of-sample ({len(oos_days)} days)…{_RST}")
    oos_results = _replay_segment(oos_days)

    _print_walkforward_report(is_results, oos_results, is_days, oos_days)


def _print_walkforward_report(
    is_results: list[DayResult],
    oos_results: list[DayResult],
    is_days: list,
    oos_days: list,
) -> None:
    """Side-by-side IS vs OOS performance comparison."""

    def _seg_stats(results: list[DayResult]) -> Optional[dict]:
        all_trades = [t for dr in results for t in dr.trades]
        if not all_trades:
            return None
        net     = sum(t.net_pnl for t in all_trades)
        wins    = [t for t in all_trades if t.net_pnl > 0]
        losses  = [t for t in all_trades if t.net_pnl <= 0]
        gw      = sum(t.net_pnl for t in wins)
        gl      = abs(sum(t.net_pnl for t in losses))
        pf      = gw / gl if gl > 0 else float("inf")
        daily   = [dr.net_pnl for dr in results]
        cumul: list[float] = []
        r = 0.0
        for v in daily:
            r += v
            cumul.append(r)
        skipped = sum(1 for dr in results if dr.env_blocked)
        return {
            "trades":   len(all_trades),
            "wins":     len(wins),
            "losses":   len(losses),
            "net":      net,
            "win_rate": len(wins) / len(all_trades) * 100,
            "pf":       pf,
            "sharpe":   _sharpe(daily),
            "max_dd":   _max_drawdown(cumul),
            "skipped":  skipped,
            "days":     len(results),
        }

    is_s  = _seg_stats(is_results)
    oos_s = _seg_stats(oos_results)

    print(f"\n  {_B}{'═'*80}{_RST}")
    print(f"  {_B}  WALK-FORWARD RESULTS  (FILTERED CONFIG){_RST}")
    print(f"  {_B}{'═'*80}{_RST}")

    def _row(label: str, is_val, oos_val, fmt: str,
             higher_is_better: bool = True,
             threshold: float = None) -> None:
        is_str  = format(is_val,  fmt) if is_val  is not None else "—"
        oos_str = format(oos_val, fmt) if oos_val is not None else "—"
        if oos_val is not None and is_val is not None and is_val != 0:
            if threshold is not None:
                pass_thresh = (oos_val >= threshold if higher_is_better
                               else oos_val <= threshold)
                oos_clr = _G if pass_thresh else _R
            else:
                holds = (oos_val >= is_val * 0.70 if higher_is_better
                         else oos_val <= is_val * 1.30)
                oos_clr = _G if holds else _R
            verdict = (f"{oos_clr}✓ HOLDS{_RST}"
                       if (oos_val >= is_val * 0.70 if higher_is_better
                           else oos_val <= is_val * 1.30)
                       else f"{_R}⚠ DEGRADES{_RST}")
        else:
            oos_clr = _D
            verdict = ""
        print(f"  {label:28s}  {is_str:>18s}  {oos_clr}{oos_str:>18s}{_RST}  {verdict}")

    if is_s and oos_s:
        print(f"\n  {'':28s}  {'IN-SAMPLE':>18s}  {'OUT-OF-SAMPLE':>18s}")
        print(f"  {'─'*80}")
        print(f"  {'Period':28s}  "
              f"{is_days[0].strftime('%d %b') + ' – ' + is_days[-1].strftime('%d %b %Y'):>18s}  "
              f"{oos_days[0].strftime('%d %b') + ' – ' + oos_days[-1].strftime('%d %b %Y'):>18s}")
        print(f"  {'─'*80}")
        _row("Trading days",     is_s["days"],     oos_s["days"],     "d",    True)
        _row("Days skipped(ENV/CHOP)", is_s["skipped"], oos_s["skipped"], "d", False)
        _row("Total trades",     is_s["trades"],   oos_s["trades"],   "d",    True)
        _row("Win rate %",       is_s["win_rate"], oos_s["win_rate"], ".1f",  True,  45.0)
        _row("Profit factor",    is_s["pf"],       oos_s["pf"],       ".2f",  True,  1.5)
        _row("Net P&L (₹)",      is_s["net"],      oos_s["net"],      "+,.0f",True,  0.0)
        _row("Daily Sharpe",     is_s["sharpe"],   oos_s["sharpe"],   "+.2f", True,  0.5)
        _row("Max drawdown (₹)", is_s["max_dd"],   oos_s["max_dd"],   ",.0f", False)
        print(f"  {'─'*80}")

        # ── Verdict ──────────────────────────────────────────────────────────
        oos_pf, oos_net, oos_wr = oos_s["pf"], oos_s["net"], oos_s["win_rate"]
        print(f"\n  {_B}  VERDICT{_RST}")
        if oos_net > 0 and oos_pf >= 1.5 and oos_wr >= 45:
            print(f"  {_G}  ✓ EDGE VALIDATED  —  OOS P&L ₹{oos_net:+,.0f}  PF {oos_pf:.2f}  WR {oos_wr:.0f}%{_RST}")
            print(f"  {_G}    Strategy holds on unseen data.  Cautious paper trading is warranted.{_RST}")
        elif oos_net > 0:
            print(f"  {_Y}  ⚠ MARGINAL EDGE  —  OOS positive but PF {oos_pf:.2f} < 1.5{_RST}")
            print(f"  {_Y}    Paper trade 3–4 more weeks before committing real capital.{_RST}")
        else:
            print(f"  {_R}  ✗ EDGE BREAKS DOWN  —  OOS P&L ₹{oos_net:+,.0f}{_RST}")
            print(f"  {_R}    Config is likely overfit to the IS window.  Do NOT live-trade.{_RST}")
            print(f"  {_D}    Run --backtest 30 to find a parameter set that survives OOS.{_RST}")
    else:
        print(f"\n  {_R}  No trades in one or both segments — cannot validate.{_RST}")

    print(f"\n  {_B}{'═'*80}{_RST}\n")
