"""
analytics/swing_backtest.py — Per-ticker Swing Strategy Validation
===================================================================
Historical replay of a long-only Donchian-breakout swing strategy on
each ticker, with realistic Zerodha fees and slippage.

Purpose: BEFORE placing real capital on a shortlisted stock, prove the
strategy has worked on THAT specific ticker over the past N days.

Why this exists: the live scanner identifies setups but has never been
back-tested per ticker. This module closes that gap.

  python run.py --swing-backtest TICKER1,TICKER2 --days 180
  python run.py --swing-backtest --shortlist     (uses today's plan)
"""

from __future__ import annotations
import contextlib
import io
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf


# ── ANSI ─────────────────────────────────────────────────────────────────────
_R, _Y, _G, _C, _D, _B, _RST = (
    "\033[91m", "\033[93m", "\033[92m",
    "\033[96m", "\033[2m",  "\033[1m", "\033[0m",
)


# ── Strategy parameters (mirror swing engine defaults) ───────────────────────
DEFAULTS = {
    "donchian_lookback":   20,        # breakout = close > rolling-20-high
    "atr_window":          14,
    "stop_atr_multiple":   1.5,
    "target_r_multiple":   2.0,       # exit at +2R
    "trail_atr_multiple":  2.0,       # after +1R, trail at close - 2*ATR
    "time_stop_days":      20,
    "failed_breakout_r":  -0.5,
    "failed_after_days":   5,
    "min_avg_volume":      300_000,
    "min_price":           50.0,
    "slippage_pct":        0.15,      # 0.15% each side, conservative for retail
    "risk_per_trade":      1000.0,    # ₹ per trade
    "capital":             100_000.0,
}

# ── Zerodha equity-delivery fees (CNC) ───────────────────────────────────────
STT_PCT      = 0.001            # 0.1% on sell side (delivery)
EXCH_TXN_PCT = 0.0000345        # NSE
GST_PCT      = 0.18             # 18% on (brokerage + exch txn)
SEBI_PCT     = 0.000001         # ₹10 / crore
STAMP_PCT    = 0.00015          # buy side only

def _fees(side: str, price: float, qty: int) -> float:
    """Zerodha CNC delivery fees one side, INR."""
    val = price * qty
    if side == "BUY":
        stamp     = val * STAMP_PCT
        exch_txn  = val * EXCH_TXN_PCT
        sebi      = val * SEBI_PCT
        gst       = (exch_txn) * GST_PCT
        return round(exch_txn + sebi + stamp + gst, 2)
    else:
        stt       = val * STT_PCT
        exch_txn  = val * EXCH_TXN_PCT
        sebi      = val * SEBI_PCT
        gst       = (exch_txn) * GST_PCT
        return round(stt + exch_txn + sebi + gst, 2)


# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class SwingTrade:
    ticker:       str
    entry_date:   pd.Timestamp
    entry_price:  float
    stop_price:   float
    target_price: float
    quantity:     int
    risk_inr:     float
    exit_date:    Optional[pd.Timestamp] = None
    exit_price:   float = 0.0
    exit_reason:  str   = ""
    pnl:          float = 0.0
    fees:         float = 0.0
    r_multiple:   float = 0.0
    days_held:    int   = 0

    @property
    def is_closed(self) -> bool:
        return self.exit_date is not None


@dataclass
class TickerStats:
    ticker:    str
    trades:    int   = 0
    wins:      int   = 0
    losses:    int   = 0
    win_rate:  float = 0.0
    pf:        float = 0.0
    avg_r:     float = 0.0
    net_pnl:   float = 0.0
    max_dd:    float = 0.0
    qualified: bool  = False
    reason:    str   = ""
    all_trades: list[SwingTrade] = field(default_factory=list)


# ── Indicators ───────────────────────────────────────────────────────────────
def _atr(df: pd.DataFrame, window: int) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc      = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(window).mean()


def _fetch_eod(ticker: str, days: int) -> pd.DataFrame:
    """Download EOD bars, return ohlcv with extras."""
    period_days = days + 90  # buffer for indicators
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        df = yf.download(
            ticker,
            period=f"{period_days}d",
            interval="1d",
            progress=False,
            auto_adjust=False,
        )
    if df is None or df.empty:
        return pd.DataFrame()
    # Flatten multi-index columns yfinance sometimes returns
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna(subset=["Close"])
    return df


# ── Single-ticker backtest ───────────────────────────────────────────────────
def _backtest_one(ticker: str, days: int, params: dict) -> TickerStats:
    df = _fetch_eod(ticker, days)
    stats = TickerStats(ticker=ticker)
    if df.empty:
        stats.reason = "no data"
        return stats
    if len(df) < params["donchian_lookback"] + params["atr_window"] + 5:
        stats.reason = "insufficient history"
        return stats

    df["DON_HIGH"] = df["High"].rolling(params["donchian_lookback"]).max().shift(1)
    df["ATR"]      = _atr(df, params["atr_window"])
    df["AVG_VOL"]  = df["Volume"].rolling(20).mean()

    # Restrict simulation window to last `days` trading days
    sim_df = df.tail(days + params["donchian_lookback"] + 5).copy()
    sim_dates = sim_df.index[params["donchian_lookback"] + params["atr_window"] + 5:]

    trades: list[SwingTrade] = []
    open_trade: Optional[SwingTrade] = None
    equity_curve = [params["capital"]]

    for i, dt in enumerate(sim_dates):
        if dt not in df.index:
            continue
        row = df.loc[dt]
        prev_idx = df.index.get_loc(dt) - 1
        if prev_idx < 0:
            continue

        # ── Manage open trade first ──────────────────────────────────────────
        if open_trade is not None:
            high   = float(row["High"])
            low    = float(row["Low"])
            close  = float(row["Close"])
            atr    = float(row["ATR"]) if not np.isnan(row["ATR"]) else 0.0
            r_unit = open_trade.entry_price - open_trade.stop_price
            held   = (dt - open_trade.entry_date).days

            # Gap-through stop: open below stop → exit at open
            day_open = float(row["Open"])
            if day_open <= open_trade.stop_price:
                exit_px = day_open
                reason  = "GAP_STOP"
            # Stop hit
            elif low <= open_trade.stop_price:
                exit_px = open_trade.stop_price
                reason  = "STOP"
            # Target hit
            elif high >= open_trade.target_price:
                exit_px = open_trade.target_price
                reason  = "TARGET"
            # Failed breakout
            elif (held <= params["failed_after_days"] and r_unit > 0 and
                  (close - open_trade.entry_price) / r_unit <= params["failed_breakout_r"]):
                exit_px = close
                reason  = "FAILED"
            # Time stop
            elif held >= params["time_stop_days"]:
                exit_px = close
                reason  = "TIME"
            else:
                exit_px = None
                reason  = ""

            if exit_px is not None:
                # Apply slippage to exit
                slip = params["slippage_pct"] / 100.0
                exit_fill = exit_px * (1 - slip)
                buy_fees  = _fees("BUY",  open_trade.entry_price, open_trade.quantity)
                sell_fees = _fees("SELL", exit_fill, open_trade.quantity)
                total_fees = buy_fees + sell_fees
                gross = (exit_fill - open_trade.entry_price) * open_trade.quantity
                pnl   = gross - total_fees
                r_unit_actual = (open_trade.entry_price - open_trade.stop_price)
                r_mult = (exit_fill - open_trade.entry_price) / r_unit_actual if r_unit_actual > 0 else 0
                open_trade.exit_date   = dt
                open_trade.exit_price  = round(exit_fill, 2)
                open_trade.exit_reason = reason
                open_trade.pnl         = round(pnl, 2)
                open_trade.fees        = round(total_fees, 2)
                open_trade.r_multiple  = round(r_mult, 2)
                open_trade.days_held   = held
                trades.append(open_trade)
                equity_curve.append(equity_curve[-1] + pnl)
                open_trade = None

        # ── New entry on this bar? ───────────────────────────────────────────
        if open_trade is None:
            don_high = row.get("DON_HIGH", np.nan)
            atr      = row.get("ATR", np.nan)
            avg_vol  = row.get("AVG_VOL", np.nan)
            close    = float(row["Close"])
            if np.isnan(don_high) or np.isnan(atr) or np.isnan(avg_vol):
                continue
            if avg_vol < params["min_avg_volume"] or close < params["min_price"]:
                continue
            # Breakout
            if close > don_high:
                slip = params["slippage_pct"] / 100.0
                entry_fill = close * (1 + slip)
                stop = entry_fill - params["stop_atr_multiple"] * float(atr)
                if stop <= 0 or stop >= entry_fill:
                    continue
                r_unit = entry_fill - stop
                target = entry_fill + params["target_r_multiple"] * r_unit
                qty    = int(params["risk_per_trade"] // r_unit)
                if qty <= 0:
                    continue
                # Position-size cap: don't exceed 33% of capital per single trade
                if qty * entry_fill > 0.33 * params["capital"]:
                    qty = int((0.33 * params["capital"]) // entry_fill)
                if qty <= 0:
                    continue
                open_trade = SwingTrade(
                    ticker=ticker,
                    entry_date=dt,
                    entry_price=round(entry_fill, 2),
                    stop_price=round(stop, 2),
                    target_price=round(target, 2),
                    quantity=qty,
                    risk_inr=round(qty * r_unit, 2),
                )

    # Close any still-open trade at last bar
    if open_trade is not None and len(sim_dates) > 0:
        last_dt = sim_dates[-1]
        last_close = float(df.loc[last_dt, "Close"])
        slip = params["slippage_pct"] / 100.0
        exit_fill = last_close * (1 - slip)
        buy_fees  = _fees("BUY",  open_trade.entry_price, open_trade.quantity)
        sell_fees = _fees("SELL", exit_fill, open_trade.quantity)
        total_fees = buy_fees + sell_fees
        gross = (exit_fill - open_trade.entry_price) * open_trade.quantity
        pnl   = gross - total_fees
        r_unit_actual = open_trade.entry_price - open_trade.stop_price
        r_mult = (exit_fill - open_trade.entry_price) / r_unit_actual if r_unit_actual > 0 else 0
        open_trade.exit_date   = last_dt
        open_trade.exit_price  = round(exit_fill, 2)
        open_trade.exit_reason = "EOP"  # end of period
        open_trade.pnl         = round(pnl, 2)
        open_trade.fees        = round(total_fees, 2)
        open_trade.r_multiple  = round(r_mult, 2)
        open_trade.days_held   = (last_dt - open_trade.entry_date).days
        trades.append(open_trade)
        equity_curve.append(equity_curve[-1] + pnl)

    # ── Aggregate stats ─────────────────────────────────────────────────────
    stats.all_trades = trades
    stats.trades = len(trades)
    if not trades:
        stats.reason = "no signals triggered"
        return stats

    wins   = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    stats.wins     = len(wins)
    stats.losses   = len(losses)
    stats.win_rate = round(len(wins) / len(trades) * 100, 1)

    gross_win  = sum(t.pnl for t in wins)
    gross_loss = abs(sum(t.pnl for t in losses))
    stats.pf      = round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf")
    stats.avg_r   = round(sum(t.r_multiple for t in trades) / len(trades), 2)
    stats.net_pnl = round(sum(t.pnl for t in trades), 2)

    eq = np.array(equity_curve)
    peak = np.maximum.accumulate(eq)
    dd   = (peak - eq)
    stats.max_dd = round(float(dd.max()), 2)

    # Qualification gates (per Section 9 of audit)
    reasons = []
    if stats.trades < 3:
        reasons.append(f"only {stats.trades} trades (<3)")
    if stats.pf < 1.3:
        reasons.append(f"PF {stats.pf} (<1.3)")
    if stats.avg_r < 0.3:
        reasons.append(f"avg R {stats.avg_r} (<0.3)")
    if any(t.r_multiple < -1.5 for t in trades):
        worst = min(t.r_multiple for t in trades)
        reasons.append(f"worst trade {worst}R (<-1.5R)")
    stats.qualified = (len(reasons) == 0)
    stats.reason = "QUALIFIED" if stats.qualified else "; ".join(reasons)
    return stats


# ─────────────────────────────────────────────────────────────────────────────
def _print_report(results: list[TickerStats], days: int) -> None:
    width = 92
    print()
    print(f"  {_B}{'═' * width}{_RST}")
    print(f"  {_B}  SWING BACKTEST  —  {days}d historical replay, Zerodha fees + 0.15% slippage{_RST}")
    print(f"  {_B}{'═' * width}{_RST}")
    print(f"  {'Ticker':<14} {'Trades':>7} {'Win%':>6} {'PF':>6} {'AvgR':>6} "
          f"{'Net ₹':>10} {'MaxDD ₹':>9} {'Verdict':<28}")
    print(f"  {_D}{'-' * width}{_RST}")

    qualified = []
    rejected  = []
    for s in sorted(results, key=lambda x: (-x.qualified, -x.pf, -x.net_pnl)):
        verdict_col = _G if s.qualified else _R
        verdict_txt = "✓ QUALIFIED" if s.qualified else f"✗ {s.reason}"
        if s.trades == 0:
            verdict_txt = f"— {s.reason}"
            verdict_col = _Y
        print(
            f"  {s.ticker:<14} {s.trades:>7} {s.win_rate:>5.1f}% "
            f"{s.pf:>6.2f} {s.avg_r:>+6.2f} "
            f"{s.net_pnl:>+10,.0f} {s.max_dd:>9,.0f} "
            f"{verdict_col}{verdict_txt[:28]:<28}{_RST}"
        )
        if s.qualified:
            qualified.append(s.ticker)
        else:
            rejected.append(s.ticker)

    print(f"  {_D}{'-' * width}{_RST}")
    print(f"  {_G}QUALIFIED for real-capital deployment ({len(qualified)}):{_RST}  "
          f"{', '.join(qualified) if qualified else '(none)'}")
    print(f"  {_R}REJECT or paper-only ({len(rejected)}):{_RST}  "
          f"{', '.join(rejected) if rejected else '(none)'}")
    print()
    print(f"  {_C}Qualification gates:{_RST} ≥3 trades, PF ≥1.3, avg R ≥+0.3, "
          f"no trade <-1.5R")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY
# ─────────────────────────────────────────────────────────────────────────────
def run_swing_backtest(
    tickers: list[str],
    days: int = 180,
    capital: float = 100_000.0,
    risk_per_trade: float = 1000.0,
) -> list[TickerStats]:
    """
    Backtest the long-only Donchian-breakout swing strategy on each ticker.
    Returns list of TickerStats, also prints a report.
    """
    params = dict(DEFAULTS)
    params["capital"]        = capital
    params["risk_per_trade"] = risk_per_trade

    print(f"\n  Backtesting {len(tickers)} ticker(s) over last {days} days...\n")
    results = []
    for t in tickers:
        t_norm = t.strip().upper()
        if not t_norm.endswith(".NS") and not t_norm.endswith(".BO"):
            t_norm = f"{t_norm}.NS"
        print(f"  ...{t_norm}", end="", flush=True)
        s = _backtest_one(t_norm, days, params)
        print(f"  → {s.trades} trades, PF {s.pf:.2f}")
        results.append(s)

    _print_report(results, days)
    _write_qualified_cache(results, days)
    return results


def _write_qualified_cache(results: list[TickerStats], days: int,
                            journal_dir: str = "Journal") -> None:
    """Persist qualified universe so `--morning` can constrain its picks."""
    import json
    from datetime import datetime
    from pathlib import Path
    Path(journal_dir).mkdir(parents=True, exist_ok=True)
    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "days":         days,
        "qualified":    [s.ticker for s in results if s.qualified],
        "stats": {
            s.ticker: {
                "trades":   s.trades,
                "pf":       s.pf,
                "avg_r":    s.avg_r,
                "win_rate": s.win_rate,
                "net_pnl":  s.net_pnl,
                "max_dd":   s.max_dd,
            } for s in results
        },
    }
    (Path(journal_dir) / "qualified_universe.json").write_text(
        json.dumps(out, indent=2)
    )
    print(f"  {_C}Qualified universe cached → "
          f"Journal/qualified_universe.json{_RST}\n")


def run_swing_backtest_shortlist(
    journal_dir: str = "journal",
    days: int = 180,
) -> list[TickerStats]:
    """
    Backtest the full watchlist (105 tickers).
    Falls back to scan_log READY tickers if watchlist import fails.
    """
    tickers: list[str] = []
    try:
        from scanner.watchlist import WATCHLIST
        tickers = list(WATCHLIST)
    except Exception:
        pass

    if not tickers:
        # Fallback: today's graded setups from scan log
        try:
            from analytics.scan_logger import load_scan_log
            df = load_scan_log(journal_dir=journal_dir)
            if df is not None and not df.empty:
                latest_date = df["scan_date"].max() if "scan_date" in df.columns else None
                if latest_date is not None:
                    df = df[df["scan_date"] == latest_date]
                if "grade" in df.columns:
                    df = df[df["grade"].astype(str).str.upper().isin({"A+", "A", "B"})]
                if "ticker" in df.columns:
                    tickers = sorted({str(t).upper() for t in df["ticker"].dropna().unique()})
        except Exception:
            pass

    if not tickers:
        print(f"\n  {_R}Could not load watchlist or scan log — cannot run backtest.{_RST}")
        return []

    print(f"\n  {_D}Universe: {len(tickers)} tickers from watchlist{_RST}")
    return run_swing_backtest(tickers, days=days)
