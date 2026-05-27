
"""
analytics/outcome_tracker.py — Trade Outcome Tracker
=====================================================
Two-stage workflow:

  Stage 1: RECORD ENTRY
    When you actually take a trade (from a scan candidate), call
    record_trade_entry() to log it to trades.csv.

  Stage 2: RESOLVE OUTCOMES
    Run resolve_outcomes() daily/weekly. It uses yfinance to fetch
    post-entry price history and determines automatically:
      - Whether T1 was hit
      - Whether stop was hit
      - Max Favorable Excursion (MFE)
      - Max Adverse Excursion (MAE)
      - Days to first exit (T1 or stop)
      - Resulting R-multiple

WHY separate entry recording from outcome resolution?
    You record the entry the day you take the trade.
    Outcomes cannot be known until time passes.
    This two-stage design avoids look-ahead bias and mirrors
    how a real trading journal works.

WHY MFE and MAE?
    MFE (Max Favorable Excursion): How high did the trade go before exit?
    If MFE >> T1 but you didn't hit T1, your targets may be too tight.

    MAE (Max Adverse Excursion): How low did the trade go before recovery?
    If MAE >> stop but trade recovered and won, your stops may be too tight.
    These two metrics diagnose stop/target placement quality over time.
"""

import os
import csv
import pandas as pd
import yfinance as yf
from datetime import datetime, date
from pathlib import Path
from typing import Optional


# ── Trade log schema ──────────────────────────────────────────────────────────
TRADE_COLUMNS = [
    # Entry fields (filled when trade is taken)
    "trade_id",          # YYYYMMDD_TICKER
    "entry_date",
    "ticker",
    "regime",
    "grade",
    "score",
    "entry_price",
    "stop_price",
    "t1",
    "t2",
    "rr_t1",
    "quantity",
    "max_loss_inr",
    "atr_pct",
    "relative_strength",
    "range_pct",
    "notes",             # free-text for your own annotations

    # Outcome fields (filled by resolve_outcomes())
    "exit_date",
    "exit_price",
    "exit_reason",       # T1_HIT / T2_HIT / STOP_HIT / MANUAL / OPEN
    "r_multiple",        # actual (exit - entry) / risk_per_share
    "mfe_pct",           # max favorable excursion % from entry
    "mae_pct",           # max adverse excursion % from entry
    "days_held",
    "resolved",          # True/False

    # Partial-exit fields (optional — populated by --partial command)
    "partial_qty",       # shares sold at first partial
    "partial_price",     # exit price of partial
    "partial_date",      # YYYY-MM-DD of partial
    "partial_r",         # R-multiple booked on partial (always positive if T1+)
    "current_stop",      # live stop (updated by --trail; defaults = stop_price)
]


def get_trade_log_path(journal_dir: str = "journal") -> Path:
    path = Path(journal_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path / "trades.csv"


# ═══════════════════════════════════════════════════
# STAGE 1 — RECORD ENTRY
# ═══════════════════════════════════════════════════
def record_trade_entry(
    plan: dict,
    regime: str,
    entry_date: Optional[str] = None,
    notes: str = "",
    journal_dir: str = "journal"
) -> str:
    """
    Records a taken trade to trades.csv.

    Args:
        plan        : trade plan dict from build_trade_plan()
        regime      : "BULL" / "NEUTRAL" / "BEAR"
        entry_date  : "YYYY-MM-DD" or None (defaults to today)
        notes       : optional free-text note (e.g. "Volume breakout confirmed")
        journal_dir : path to journal folder

    Returns:
        trade_id string.

    Usage:
        from analytics.outcome_tracker import record_trade_entry
        record_trade_entry(plan, regime["regime"], notes="Volume spike on entry bar")
    """
    if entry_date is None:
        entry_date = date.today().strftime("%Y-%m-%d")

    ticker   = plan["ticker"]
    trade_id = f"{entry_date.replace('-','')}_{ticker.replace('.NS','')}"

    log_path    = get_trade_log_path(journal_dir)
    write_header = not log_path.exists() or log_path.stat().st_size == 0

    row = {col: "" for col in TRADE_COLUMNS}
    row.update({
        "trade_id":          trade_id,
        "entry_date":        entry_date,
        "ticker":            ticker,
        "regime":            regime,
        "grade":             plan.get("grade", ""),
        "score":             plan.get("score", ""),
        "entry_price":       plan.get("entry_price", ""),
        "stop_price":        plan.get("stop_price", ""),
        "t1":                plan.get("t1", ""),
        "t2":                plan.get("t2", ""),
        "rr_t1":             plan.get("rr_t1", ""),
        "quantity":          plan.get("quantity", ""),
        "max_loss_inr":      plan.get("max_loss_inr", ""),
        "atr_pct":           plan.get("atr_pct", ""),
        "relative_strength": plan.get("relative_strength", ""),
        "range_pct":         plan.get("range_pct", ""),
        "notes":             notes,
        "resolved":          "False",
        "exit_reason":       "OPEN",
    })

    with open(log_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_COLUMNS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)

    print(f"  ✓ Trade logged: {trade_id} | Entry ₹{plan.get('entry_price')} | Stop ₹{plan.get('stop_price')}")
    return trade_id


# ═══════════════════════════════════════════════════
# STAGE 2 — RESOLVE OUTCOMES
# ═══════════════════════════════════════════════════
def _fetch_post_entry_prices(ticker: str, entry_date: str,
                              max_days: int = 60) -> Optional[pd.DataFrame]:
    """
    Fetches OHLCV data from entry_date onwards (up to max_days bars).
    Returns None if data unavailable.

    WHY max_days=60?
        A swing trade that hasn't resolved in 60 trading days (~3 months)
        should be reviewed manually. Time decay on setups is real.
    """
    df = yf.download(ticker, start=entry_date, auto_adjust=True, progress=False)
    if df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()
    return df.head(max_days) if len(df) > max_days else df


def _resolve_single_trade(row: pd.Series) -> dict:
    """
    Given a single unresolved trade row, fetch post-entry data
    and determine outcome.

    Resolution logic (in priority order):
      1. Stop hit first  → STOP_HIT, R = -1.0
      2. T1 hit first    → T1_HIT,   R = +rr_t1
      3. T2 hit first    → T2_HIT,   R = +rr_t2
      4. Neither yet     → OPEN (skip)

    WHY check DAILY LOWS for stop, DAILY HIGHS for targets?
        This is the most realistic intraday resolution without
        tick-level data. Low-of-day can trigger a stop;
        high-of-day can reach a target on the same bar.
        This gives a conservative (not optimistic) outcome picture.
    """
    ticker      = row["ticker"]
    entry_date  = str(row["entry_date"])
    entry_price = float(row["entry_price"])
    stop_price  = float(row["stop_price"])
    t1          = float(row["t1"])
    t2          = float(row["t2"])
    rr_t1       = float(row["rr_t1"]) if row["rr_t1"] != "" else 2.0

    df = _fetch_post_entry_prices(ticker, entry_date)

    if df is None or df.empty:
        return {}   # Cannot resolve — leave as OPEN

    # Skip the entry bar itself (bar 0 = entry day) — outcomes start next day
    # WHY: entry bar's high/low includes pre-entry action.
    price_data = df.iloc[1:] if len(df) > 1 else df

    if price_data.empty:
        return {}

    # ── MFE and MAE (over the full holding window) ─────────────────────────
    highest_high = float(price_data["High"].max())
    lowest_low   = float(price_data["Low"].min())
    mfe_pct      = round(((highest_high - entry_price) / entry_price) * 100, 2)
    mae_pct      = round(((lowest_low  - entry_price) / entry_price) * 100, 2)

    # ── Bar-by-bar resolution: find FIRST exit ──────────────────────────────
    exit_date   = None
    exit_price  = None
    exit_reason = "OPEN"

    for i, (idx, bar) in enumerate(price_data.iterrows()):
        bar_low  = float(bar["Low"])
        bar_high = float(bar["High"])

        # Stop hit: low of bar touches/pierces stop
        stop_hit = bar_low <= stop_price
        # T1 hit:   high of bar reaches T1
        t1_hit   = bar_high >= t1
        # T2 hit:   high of bar reaches T2
        t2_hit   = bar_high >= t2

        # Same-bar resolution: if stop and target hit on same bar,
        # be conservative — assume stop hit first (worst-case)
        if stop_hit and (t1_hit or t2_hit):
            stop_hit = True
            t1_hit   = False
            t2_hit   = False

        if t2_hit:
            exit_date   = idx.strftime("%Y-%m-%d")
            exit_price  = t2
            exit_reason = "T2_HIT"
            days_held   = i + 1
            r_multiple  = round(rr_t1 * 1.5, 2)  # ~3R if T2 was at 3R
            break
        elif t1_hit:
            exit_date   = idx.strftime("%Y-%m-%d")
            exit_price  = t1
            exit_reason = "T1_HIT"
            days_held   = i + 1
            r_multiple  = round(rr_t1, 2)
            break
        elif stop_hit:
            exit_date   = idx.strftime("%Y-%m-%d")
            exit_price  = stop_price
            exit_reason = "STOP_HIT"
            days_held   = i + 1
            r_multiple  = -1.0
            break
    else:
        # Loop completed without exit — still open
        return {
            "mfe_pct":   mfe_pct,
            "mae_pct":   mae_pct,
            "resolved":  "False",
            "exit_reason": "OPEN",
        }

    return {
        "exit_date":   exit_date,
        "exit_price":  exit_price,
        "exit_reason": exit_reason,
        "r_multiple":  r_multiple,
        "mfe_pct":     mfe_pct,
        "mae_pct":     mae_pct,
        "days_held":   days_held,
        "resolved":    "True",
    }


def resolve_outcomes(journal_dir: str = "journal",
                     force_recheck: bool = False) -> dict:
    """
    Iterates all unresolved trades and attempts to resolve them via yfinance.

    Args:
        journal_dir   : path to journal folder
        force_recheck : if True, re-resolves all trades (not just OPEN ones).
                        Useful if you want to re-run after fixing a bug.

    Returns:
        Summary dict with counts of newly resolved trades.

    Usage:
        python -c "from analytics.outcome_tracker import resolve_outcomes; resolve_outcomes()"
    """
    log_path = get_trade_log_path(journal_dir)
    if not log_path.exists():
        print("  No trades.csv found — no trades to resolve.")
        return {"resolved": 0, "still_open": 0, "errors": 0}

    df = pd.read_csv(log_path)

    numeric_cols = ["entry_price", "stop_price", "t1", "t2", "rr_t1",
                    "quantity", "max_loss_inr", "atr_pct", "relative_strength"]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Select rows to process
    if force_recheck:
        to_process = df.copy()
    else:
        to_process = df[df["resolved"].astype(str) != "True"].copy()

    resolved_count  = 0
    still_open      = 0
    errors          = 0
    _newly_resolved = []  # (ticker, row_dict, outcome) for Telegram exit notifications

    print(f"\n  Resolving {len(to_process)} open trade(s)...")

    for idx, row in to_process.iterrows():
        ticker   = row["ticker"]
        was_open = str(row.get("resolved", "")) != "True"
        print(f"    {ticker:<14}", end=" ", flush=True)

        try:
            outcome = _resolve_single_trade(row)

            if not outcome:
                print("⚠  no price data")
                errors += 1
                continue

            if outcome.get("resolved") == "True":
                for col, val in outcome.items():
                    df.at[idx, col] = val
                print(f"✓  {outcome['exit_reason']:<10} R={outcome.get('r_multiple', '?'):>5}")
                resolved_count += 1
                if was_open:
                    _newly_resolved.append((ticker, dict(row), outcome))
            else:
                # Update MFE/MAE even if still open
                for col in ["mfe_pct", "mae_pct", "exit_reason"]:
                    if col in outcome:
                        df.at[idx, col] = outcome[col]
                print(f"⏳ OPEN  (MFE:{outcome.get('mfe_pct','?'):>6}%  MAE:{outcome.get('mae_pct','?'):>6}%)")
                still_open += 1

        except Exception as e:
            print(f"✗  error: {e}")
            errors += 1

    # Write updated file back
    df.to_csv(log_path, index=False)

    # ── Telegram exit notifications for freshly resolved trades ──────────────
    if _newly_resolved:
        try:
            final_open = int((df["resolved"].astype(str) != "True").sum())
            from integrations.telegram_notifier import notify_sl_hit, notify_target_hit
            for _ticker, _row, _outcome in _newly_resolved:
                try:
                    _entry  = float(_row.get("entry_price", 0) or 0)
                    _exit   = float(_outcome.get("exit_price", 0) or 0)
                    _qty    = int(float(_row.get("quantity", 0) or 0))
                    _r      = float(_outcome.get("r_multiple", 0) or 0)
                    _pnl    = round((_exit - _entry) * _qty, 0)
                    _reason = _outcome.get("exit_reason", "")
                    if _reason == "STOP_HIT":
                        notify_sl_hit(
                            ticker=_ticker, exit_price=_exit,
                            r_multiple=_r, loss_inr=abs(_pnl),
                            open_trades=final_open,
                        )
                    elif _reason in ("T1_HIT", "T2_HIT"):
                        notify_target_hit(
                            ticker=_ticker, exit_price=_exit,
                            r_multiple=_r, profit_inr=_pnl,
                            open_trades=final_open,
                        )
                except Exception:
                    pass
        except Exception:
            pass

    summary = {
        "resolved":   resolved_count,
        "still_open": still_open,
        "errors":     errors,
    }
    print(f"\n  Resolution complete: {resolved_count} resolved | "
          f"{still_open} still open | {errors} errors")
    return summary


def load_trade_log(journal_dir: str = "journal") -> pd.DataFrame:
    """
    Loads trades.csv with proper type coercions.
    Returns empty DataFrame if no trades exist yet (file missing OR empty).
    """
    log_path = get_trade_log_path(journal_dir)

    if not log_path.exists() or log_path.stat().st_size == 0:
        return pd.DataFrame(columns=TRADE_COLUMNS)

    try:
        df = pd.read_csv(log_path, parse_dates=["entry_date", "exit_date"])
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=TRADE_COLUMNS)

    numeric_cols = [
        "score", "entry_price", "stop_price", "t1", "t2", "rr_t1",
        "quantity", "max_loss_inr", "atr_pct", "relative_strength",
        "range_pct", "exit_price", "r_multiple", "mfe_pct", "mae_pct",
        "days_held",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


