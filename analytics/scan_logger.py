"""
analytics/scan_logger.py — Scan Result Persistence Layer
=========================================================
Saves every scan's trade plans to a daily CSV log.
This is the raw data source for all downstream analytics.

Design principles:
  - Append-only: never modify historical records
  - Idempotent: re-running the scanner the same day adds a new timestamped
    block but does NOT overwrite prior entries. This preserves intraday changes.
  - Flat CSV: human-readable, survives any future code changes, importable
    into Excel/Sheets without tooling.
  - Schema is explicit and versioned in SCAN_LOG_COLUMNS.
  - Atomic writes: every append uses temp → fsync → rename so a crash
    or Ctrl+C mid-write never corrupts the CSV.

WHY log everything, not just trades you took?
  Because the counterfactual matters. If you only log taken trades,
  you can never answer: "Was my filter actually helpful or did it
  just reduce my sample size?" The raw scan log is the ground truth.
"""

import csv
import pandas as pd
from datetime import datetime
from pathlib import Path

from analytics.journal_writer import atomic_append_rows, cleanup_orphaned_temp


# ── Schema (column order in CSV) ─────────────────────────────────────────────
SCAN_LOG_COLUMNS = [
    "scan_timestamp",    # ISO datetime of the scan
    "scan_date",         # YYYY-MM-DD for easy date-based filtering
    "ticker",
    "regime",            # BULL / NEUTRAL / BEAR
    "regime_strength",   # 0.0–1.0
    "score",             # 0–100 weighted score
    "grade",             # A+ / A / B / AVOID
    "status",            # READY / WATCH / EXTENDED / AVOID
    "entry_price",
    "stop_price",
    "t1",
    "t2",
    "measured_move",
    "rr_t1",
    "rr_t2",
    "risk_per_share",
    "risk_pct",
    "quantity",
    "capital_deployed",
    "max_loss_inr",
    "atr_pct",
    "relative_strength",
    "range_pct",         # consolidation range %
    "volume_ratio",
    "trend_score",       # 0–4 SMA conditions
    "breakout_level",
    "extension_pct",
    "current_close",
]


def get_scan_log_path(journal_dir: str = "journal") -> Path:
    """Returns path to the scan log CSV. Creates directory if needed."""
    path = Path(journal_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path / "scan_log.csv"


def log_scan_results(
    plans: list,
    regime: dict,
    journal_dir: str = "journal"
) -> int:
    """
    Appends scan results to scan_log.csv atomically.

    Args:
        plans       : list of trade plan dicts from run_scanner()
        regime      : regime dict from get_market_regime()
        journal_dir : path to journal folder

    Returns:
        Number of rows written.

    Notes:
        - Creates the CSV with headers if it doesn't exist.
        - Appends rows (never overwrites).
        - Missing keys default to empty string — future-proofs schema changes.
        - Write is atomic: a crash during append leaves the original intact.
    """
    log_path = get_scan_log_path(journal_dir)
    # Remove any .tmp left by a previous crash before appending
    cleanup_orphaned_temp(log_path)

    now      = datetime.now()
    ts       = now.strftime("%Y-%m-%d %H:%M:%S")
    date_str = now.strftime("%Y-%m-%d")

    rows = []
    for plan in plans:
        row = {col: "" for col in SCAN_LOG_COLUMNS}
        row.update({
            "scan_timestamp":   ts,
            "scan_date":        date_str,
            "ticker":           plan.get("ticker", ""),
            "regime":           regime.get("regime", ""),
            "regime_strength":  regime.get("strength", ""),
            "score":            plan.get("score", ""),
            "grade":            plan.get("grade", ""),
            "status":           plan.get("status", ""),
            "entry_price":      plan.get("entry_price", ""),
            "stop_price":       plan.get("stop_price", ""),
            "t1":               plan.get("t1", ""),
            "t2":               plan.get("t2", ""),
            "measured_move":    plan.get("measured_move", ""),
            "rr_t1":            plan.get("rr_t1", ""),
            "rr_t2":            plan.get("rr_t2", ""),
            "risk_per_share":   plan.get("risk_per_share", ""),
            "risk_pct":         plan.get("risk_pct", ""),
            "quantity":         plan.get("quantity", ""),
            "capital_deployed": plan.get("capital_deployed", ""),
            "max_loss_inr":     plan.get("max_loss_inr", ""),
            "atr_pct":          plan.get("atr_pct", ""),
            "relative_strength": plan.get("relative_strength", ""),
            "range_pct":        plan.get("range_pct", ""),
            "volume_ratio":     plan.get("volume_ratio", ""),
            "trend_score":      plan.get("trend_score", ""),
            "breakout_level":   plan.get("breakout_level", ""),
            "extension_pct":    plan.get("extension_pct", ""),
            "current_close":    plan.get("current_close", ""),
        })
        rows.append(row)

    atomic_append_rows(log_path, rows, SCAN_LOG_COLUMNS)
    return len(rows)


def load_scan_log(journal_dir: str = "journal") -> pd.DataFrame:
    """
    Loads the full scan log CSV into a clean DataFrame.

    Handles:
      - Missing file (returns empty DataFrame with correct columns)
      - Type coercions (numeric columns parsed correctly)
      - Datetime parsing for scan_timestamp
    """
    log_path = get_scan_log_path(journal_dir)

    if not log_path.exists():
        return pd.DataFrame(columns=SCAN_LOG_COLUMNS)

    df = pd.read_csv(log_path, parse_dates=["scan_timestamp"])

    # Coerce numeric columns — CSV stores everything as strings
    numeric_cols = [
        "regime_strength", "score", "entry_price", "stop_price",
        "t1", "t2", "measured_move", "rr_t1", "rr_t2",
        "risk_per_share", "risk_pct", "quantity", "capital_deployed",
        "max_loss_inr", "atr_pct", "relative_strength",
        "range_pct", "volume_ratio", "trend_score",
        "breakout_level", "extension_pct", "current_close",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def get_scan_summary(journal_dir: str = "journal") -> dict:
    """
    Returns high-level stats about the scan log — useful for a
    quick 'how much data do we have?' health check.
    """
    df = load_scan_log(journal_dir)

    if df.empty:
        return {"total_scans": 0, "unique_dates": 0, "total_rows": 0}

    return {
        "total_rows":     len(df),
        "unique_dates":   df["scan_date"].nunique(),
        "unique_tickers": df["ticker"].nunique(),
        "grade_counts":   df["grade"].value_counts().to_dict(),
        "status_counts":  df["status"].value_counts().to_dict(),
        "date_range":     f"{df['scan_date'].min()} → {df['scan_date'].max()}",
    }