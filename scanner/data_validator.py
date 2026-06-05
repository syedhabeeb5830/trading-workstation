"""
scanner/data_validator.py — OHLCV data integrity validation.

Detects four classes of bad data before they corrupt scan logic:

  1. DUPLICATE BARS  — same date appears multiple times in the index.
     Auto-corrected by clean_ohlcv() (last value wins).

  2. INVALID OHLC   — structural impossibilities that indicate corrupt data:
       • High < Low
       • Any of Open/High/Low/Close ≤ 0  (zero or negative price)
       • Close outside [Low, High] range   (warns, does not reject)
       • Open  outside [Low, High] range   (warns, does not reject)

  3. STALE CANDLES   — last bar is more than N *business* days old.
     A yfinance fetch that silently returns week-old data due to a
     caching or API glitch is flagged here before it distorts signals.

  4. MISSING BARS    — calendar gap between consecutive bars exceeds
     max_gap_days.  Accounts for weekends automatically; a gap larger
     than the threshold suggests a data hole rather than a holiday.

Usage:
    from scanner.data_validator import validate_ohlcv, clean_ohlcv

    df  = clean_ohlcv(raw_df)              # fix duplicates first
    res = validate_ohlcv(df, ticker="RELIANCE.NS")
    if not res.valid:
        return None                         # drop the ticker
    for w in res.warnings:
        log.warning(w)

Integration notes:
  - validate_ohlcv() is called inside fetch_stock_data() so all downstream
    functions always receive clean data.
  - The function never raises; it returns a ValidationResult with .valid=False
    on critical errors and .warnings for non-critical anomalies.
  - All thresholds have sensible defaults but are overridable for tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ValidationResult:
    """Outcome of a single validate_ohlcv() call."""
    valid:    bool          = True
    errors:   list[str]     = field(default_factory=list)
    warnings: list[str]     = field(default_factory=list)
    ticker:   str           = ""

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)
        self.valid = False

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)

    def __str__(self) -> str:
        tag    = f"[{self.ticker}] " if self.ticker else ""
        parts  = [f"{tag}valid={self.valid}"]
        if self.errors:
            parts.append("errors: " + "; ".join(self.errors))
        if self.warnings:
            parts.append("warnings: " + "; ".join(self.warnings))
        return "  |  ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Cleaner
# ─────────────────────────────────────────────────────────────────────────────

def clean_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """
    Return a deduplicated copy of *df* (last row wins per date).
    Also sorts by index ascending so callers can rely on chronological order.
    Does NOT fix OHLC relationship errors — those are validation failures.
    """
    if df is None or df.empty:
        return df
    df = df.sort_index()
    if df.index.duplicated().any():
        df = df[~df.index.duplicated(keep="last")]
    return df


# ─────────────────────────────────────────────────────────────────────────────
# Validator
# ─────────────────────────────────────────────────────────────────────────────

def validate_ohlcv(
    df: pd.DataFrame,
    ticker: str                = "",
    max_stale_business_days: int = 5,
    max_gap_calendar_days: int   = 10,
) -> ValidationResult:
    """
    Run all four integrity checks on *df*.

    Parameters
    ----------
    df                      : cleaned OHLCV DataFrame (daily bars, DatetimeIndex)
    ticker                  : name used in error messages
    max_stale_business_days : reject if last bar is older than this many bdays
    max_gap_calendar_days   : warn if any consecutive-bar gap exceeds this

    Returns
    -------
    ValidationResult — inspect .valid, .errors, .warnings
    """
    result = ValidationResult(ticker=ticker)
    tag    = f"[{ticker}]" if ticker else ""

    # ── Guard: empty ─────────────────────────────────────────────────────────
    if df is None or df.empty:
        result.add_error(f"{tag} Empty DataFrame")
        return result

    required = {"Open", "High", "Low", "Close"}
    missing  = required - set(df.columns)
    if missing:
        result.add_error(f"{tag} Missing columns: {missing}")
        return result

    # ── 1. Duplicate bars (should be fixed before calling, but double-check) ─
    n_dupes = int(df.index.duplicated().sum())
    if n_dupes:
        result.add_error(f"{tag} {n_dupes} duplicate date(s) in index")

    # ── 2. OHLC relationship validity ────────────────────────────────────────
    # 2a. High < Low (structural corruption — hard error)
    bad_hl = df["High"] < df["Low"]
    if bad_hl.any():
        n = int(bad_hl.sum())
        result.add_error(f"{tag} {n} bar(s) where High < Low")

    # 2b. Zero or negative prices (corrupt or delisted — hard error)
    bad_neg = (
        (df["Open"]  <= 0) |
        (df["High"]  <= 0) |
        (df["Low"]   <= 0) |
        (df["Close"] <= 0)
    )
    if bad_neg.any():
        n = int(bad_neg.sum())
        result.add_error(f"{tag} {n} bar(s) with zero/negative price")

    # 2c. Close outside [Low, High] (data anomaly — warn, don't reject)
    bad_close = (df["Close"] < df["Low"]) | (df["Close"] > df["High"])
    if bad_close.any():
        n = int(bad_close.sum())
        result.add_warning(f"{tag} {n} bar(s) where Close ∉ [Low, High]")

    # 2d. Open outside [Low, High] (warn only)
    bad_open = (df["Open"] < df["Low"]) | (df["Open"] > df["High"])
    if bad_open.any():
        n = int(bad_open.sum())
        result.add_warning(f"{tag} {n} bar(s) where Open ∉ [Low, High]")

    # ── 3. Stale candles ─────────────────────────────────────────────────────
    try:
        last_ts: datetime = _to_datetime(df.index[-1])
        today             = datetime.now().replace(hour=0, minute=0, second=0,
                                                    microsecond=0)
        bdays_old = _count_business_days(last_ts.date(), today.date())
        if bdays_old > max_stale_business_days:
            result.add_error(
                f"{tag} Last bar is {bdays_old} business days old "
                f"(stale; max={max_stale_business_days})"
            )
    except Exception as exc:
        result.add_warning(f"{tag} Could not check staleness: {exc}")

    # ── 4. Missing bars (large gaps) ────────────────────────────────────────
    if len(df) > 1:
        try:
            idx   = pd.DatetimeIndex(df.index).normalize()
            diffs = idx.to_series().diff().dropna()
            large = diffs[diffs > pd.Timedelta(days=max_gap_calendar_days)]
            if not large.empty:
                result.add_warning(
                    f"{tag} {len(large)} gap(s) > {max_gap_calendar_days} "
                    f"calendar days in price history "
                    f"(largest: {large.max().days}d)"
                )
        except Exception as exc:
            result.add_warning(f"{tag} Could not check bar gaps: {exc}")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _to_datetime(ts) -> datetime:
    """Normalise a pandas Timestamp / numpy datetime64 / date to datetime."""
    if isinstance(ts, pd.Timestamp):
        return ts.to_pydatetime().replace(tzinfo=None)
    if hasattr(ts, "item"):            # numpy datetime64
        return pd.Timestamp(ts).to_pydatetime().replace(tzinfo=None)
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=None)
    return datetime.combine(ts, datetime.min.time())


def _count_business_days(start, end) -> int:
    """
    Count business days (Mon–Fri) between *start* and *end* dates,
    inclusive of *end* and exclusive of *start*.

    Uses numpy.busday_count for accuracy; falls back to calendar subtraction.
    """
    try:
        return int(np.busday_count(start, end))
    except Exception:
        return max(0, (end - start).days)
