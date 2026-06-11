"""
analytics/opportunity_tracker.py — Opportunity Cost Analytics
=============================================================
Answers the question: are your filters helping or hurting?

Two-stage workflow:
  Stage 1: Candidates already logged by scan_logger.py into scan_log.csv.
           SetupStore deduplicates these into unique (strategy, ticker) setups.
           Each unique setup gets ONE setup_id — repeated daily scans update
           the SAME record rather than creating N observations.

  Stage 2: resolve_opportunity_outcomes()
           For each unique setup, fetches post-scan price data via yfinance
           starting from first_seen and determines:
             - Max Favorable Excursion (MFE %)
             - Max Adverse Excursion (MAE %)
             - Achieved R-multiple
             - Final outcome (T1_HIT / T2_HIT / STOP_HIT / OPEN)
           Results written atomically to Journal/opportunity_log.csv.

  Report:  compute_opportunity_stats() groups by oc_status bucket:
             READY / WATCH / REJECTED
           and returns PF, expectancy, win rate for each group.
           Counts are based on unique setup_ids, never daily observations.
           Rendered by print_opportunity_cost_report() in reports.py.
           Triggered by: python run.py --report opportunity-cost

WHY unique setups matter:
  If AAPL appeared in the scanner on 5 consecutive days before triggering,
  the OLD system counted 5 observations. The NEW system counts 1 setup.
  Opportunity cost metrics now reflect unique trade opportunities, not scan
  frequency.

WHY first-scan-of-day deduplication for scan_log replay?
  When replaying scan_log to seed SetupStore, we use the first scan of each
  (ticker, date) to avoid look-ahead bias from intraday rescans.
"""

from __future__ import annotations

import pandas as pd
import yfinance as yf
from pathlib import Path
from typing import Optional

from analytics.journal_writer import atomic_csv_write, cleanup_orphaned_temp
from analytics.scan_logger import load_scan_log
from analytics.setup_store import SetupStore, DEFAULT_EXPIRY_DAYS


# ── Schema ────────────────────────────────────────────────────────────────────
OPP_COLUMNS = [
    "setup_id",        # unique setup identifier from SetupStore
    "ticker",
    "strategy",
    "first_seen",      # date setup first appeared (replaces scan_date for dedup)
    "last_seen",       # most recent scan date that touched this setup
    "days_active",     # trading days the setup was active before resolution
    "score",           # score at first_seen
    "raw_status",      # scanner status at first_seen: READY/WATCH/AVOID/EXTENDED/GAPPED
    "oc_status",       # bucketed: READY / WATCH / REJECTED
    "entry_price",
    "stop_price",
    "t1",
    "t2",
    "rr_t1",
    # Outcome fields (filled by resolve_opportunity_outcomes)
    "mfe_pct",         # max favourable excursion % from entry (over 60-day window)
    "mae_pct",         # max adverse excursion % from entry
    "r_multiple",      # achieved R: +rr_t1 for T1, +rr_t1*1.5 for T2, -1.0 for stop
    "final_outcome",   # T1_HIT / T2_HIT / STOP_HIT / OPEN
    "resolved",        # True / False
]

MIN_SAMPLE = 5   # minimum resolved setups for meaningful stats

_DEFAULT_STRATEGY = "SWING"


# ── Status mapping ────────────────────────────────────────────────────────────

def _map_oc_status(raw_status: str) -> str:
    """Map scanner raw status → opportunity cost bucket (READY/WATCH/REJECTED)."""
    s = str(raw_status).upper().strip()
    if s == "READY":
        return "READY"
    if s in ("WATCH", "EXTENDED"):
        return "WATCH"
    return "REJECTED"


def get_opportunity_log_path(journal_dir: str = "journal") -> Path:
    path = Path(journal_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path / "opportunity_log.csv"


# ═══════════════════════════════════════════════════════
# CANDIDATE INDEX — unique setups from scan_log
# ═══════════════════════════════════════════════════════

def _build_candidate_index(
    scan_df: pd.DataFrame,
    strategy: str = _DEFAULT_STRATEGY,
) -> pd.DataFrame:
    """
    From the full append-only scan_log, build ONE record per unique
    (strategy, ticker) setup using SetupStore deduplication logic.

    This is a read-only replay used during resolve — it does NOT persist
    the SetupStore. The live SetupStore (updated by daily scans) lives in
    Journal/setup_store.json.

    Returns:
        DataFrame with OPP_COLUMNS schema. Each row = one unique setup.
        scan_date column uses first_seen date for backward compatibility.
    """
    if scan_df.empty:
        return pd.DataFrame(columns=OPP_COLUMNS)

    df = scan_df.copy()

    if "scan_timestamp" in df.columns:
        df["scan_timestamp"] = pd.to_datetime(df["scan_timestamp"], errors="coerce")
        df = df.sort_values("scan_timestamp")

    # One record per (ticker, scan_date) — earliest scan of that day
    df = df.drop_duplicates(subset=["ticker", "scan_date"], keep="first")

    for col in ["entry_price", "stop_price", "t1", "t2", "rr_t1", "score"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["entry_price", "stop_price", "t1"])
    df = df[df["entry_price"] > 0]

    if df.empty:
        return pd.DataFrame(columns=OPP_COLUMNS)

    # Replay into an in-memory SetupStore to derive unique setups
    store = _MemorySetupStore()

    for _, row in df.iterrows():
        ticker      = str(row.get("ticker", ""))
        scan_date   = str(row.get("scan_date", ""))
        raw_status  = str(row.get("status", "AVOID"))
        entry       = float(row.get("entry_price", 0) or 0)
        stop        = float(row.get("stop_price",  0) or 0)
        score       = row.get("score", "")
        t1          = row.get("t1", "")
        t2          = row.get("t2", "")
        rr_t1       = row.get("rr_t1", "")

        if entry <= 0 or stop <= 0:
            continue

        sid, created = store.upsert(
            ticker=ticker, strategy=strategy,
            entry=entry, stop=stop,
            scan_status=raw_status, scan_date=scan_date,
        )

        if created:
            store.meta[sid] = {
                "score":       score,
                "raw_status":  raw_status,
                "oc_status":   _map_oc_status(raw_status),
                "entry_price": entry,
                "stop_price":  stop,
                "t1":          t1,
                "t2":          t2,
                "rr_t1":       rr_t1,
            }

    rows = []
    for sid, s in store.setups.items():
        meta = store.meta.get(sid, {})
        rows.append({
            "setup_id":    sid,
            "ticker":      s["ticker"],
            "strategy":    s["strategy"],
            "first_seen":  s["first_seen"],
            "last_seen":   s["last_seen"],
            "days_active": s["days_active"],
            "score":       meta.get("score", ""),
            "raw_status":  meta.get("raw_status", ""),
            "oc_status":   meta.get("oc_status", ""),
            "entry_price": meta.get("entry_price", ""),
            "stop_price":  meta.get("stop_price", ""),
            "t1":          meta.get("t1", ""),
            "t2":          meta.get("t2", ""),
            "rr_t1":       meta.get("rr_t1", ""),
            "mfe_pct":     "",
            "mae_pct":     "",
            "r_multiple":  "",
            "final_outcome": "OPEN",
            "resolved":    "False",
        })

    return pd.DataFrame(rows, columns=OPP_COLUMNS)


class _MemorySetupStore:
    """
    Lightweight in-memory SetupStore used only for scan_log replay.
    Mirrors SetupStore.upsert_setup() logic without file I/O.
    """

    def __init__(self) -> None:
        from analytics.setup_store import _SCANNER_TO_SETUP, ACTIVE_STATUSES, _STATUS_RANK, _trading_days_between
        self._map       = _SCANNER_TO_SETUP
        self._active    = ACTIVE_STATUSES
        self._rank      = _STATUS_RANK
        self._tdays     = _trading_days_between
        self.setups: dict[str, dict] = {}
        self.meta:   dict[str, dict] = {}
        self._index: dict[str, str]  = {}   # "strategy|ticker" → setup_id

    def upsert(
        self,
        ticker: str, strategy: str,
        entry: float, stop: float,
        scan_status: str, scan_date: str,
    ) -> tuple[str, bool]:
        new_status = self._map.get(scan_status.upper().strip(), "WATCH")
        key = f"{strategy}|{ticker}"

        if key in self._index:
            sid = self._index[key]
            s   = self.setups[sid]
            if new_status in self._rank and s["status"] in self._rank:
                if self._rank[new_status] > self._rank[s["status"]]:
                    s["status"] = new_status
            elif new_status not in self._rank:
                s["status"]     = new_status
                s["resolution"] = scan_status
                del self._index[key]
            s["last_seen"]   = scan_date
            s["entry"]       = round(entry, 2)
            s["stop"]        = round(stop,  2)
            s["days_active"] = self._tdays(s["first_seen"], scan_date)
            self.setups[sid] = s
            return sid, False

        base = f"{strategy}_{ticker}_{scan_date}".replace(".", "_")
        sid  = base
        c    = 1
        while sid in self.setups:
            sid = f"{base}_{c}"; c += 1

        self.setups[sid] = {
            "setup_id":   sid,
            "ticker":     ticker,
            "strategy":   strategy,
            "first_seen": scan_date,
            "last_seen":  scan_date,
            "entry":      round(entry, 2),
            "stop":       round(stop,  2),
            "status":     new_status,
            "days_active": 1,
            "resolution": "",
        }
        if new_status in self._active:
            self._index[key] = sid
        return sid, True


# ═══════════════════════════════════════════════════════
# PRICE FETCH
# ═══════════════════════════════════════════════════════

def _fetch_prices(ticker: str, start_date: str,
                  max_days: int = 60) -> Optional[pd.DataFrame]:
    """Fetch up to max_days bars of OHLCV from start_date onwards."""
    try:
        df = yf.download(ticker, start=start_date, auto_adjust=True, progress=False)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()
    return df.head(max_days) if len(df) > max_days else df


# ═══════════════════════════════════════════════════════
# BAR-BY-BAR RESOLUTION
# ═══════════════════════════════════════════════════════

def _resolve_candidate(row: pd.Series) -> dict:
    """
    Bar-by-bar resolution for a unique setup candidate.

    Conservative same-bar rule: if stop and target both hit on the same bar,
    assume stop hit first (worst-case).

    Outcomes start the day AFTER first_seen (scan day is excluded).
    """
    try:
        entry_price = float(row["entry_price"])
        stop_price  = float(row["stop_price"])
        t1          = float(row["t1"])
        t2_raw      = row.get("t2", "")
        t2          = float(t2_raw) if str(t2_raw) not in ("", "nan") else t1 * 1.5
        rr_t1       = float(row["rr_t1"]) if str(row.get("rr_t1", "")) not in ("", "nan") else 2.0
        # Use first_seen as the start date for price fetch
        start_date  = str(row.get("first_seen") or row.get("scan_date", ""))
        ticker      = str(row["ticker"])
    except (ValueError, TypeError):
        return {}

    if entry_price <= 0 or stop_price <= 0 or t1 <= 0:
        return {}

    df = _fetch_prices(ticker, start_date)
    if df is None or df.empty:
        return {}

    # Exclude the scan-day bar — outcomes begin next trading day
    price_data = df.iloc[1:] if len(df) > 1 else df
    if price_data.empty:
        return {}

    highest_high = float(price_data["High"].max())
    lowest_low   = float(price_data["Low"].min())
    mfe_pct      = round(((highest_high - entry_price) / entry_price) * 100, 2)
    mae_pct      = round(((lowest_low  - entry_price) / entry_price) * 100, 2)

    final_outcome = "OPEN"
    r_multiple    = None

    for _, bar in price_data.iterrows():
        bar_low  = float(bar["Low"])
        bar_high = float(bar["High"])

        stop_hit = bar_low  <= stop_price
        t1_hit   = bar_high >= t1
        t2_hit   = bar_high >= t2

        if stop_hit and (t1_hit or t2_hit):
            t1_hit = False
            t2_hit = False

        if t2_hit:
            final_outcome = "T2_HIT"
            r_multiple    = round(rr_t1 * 1.5, 2)
            break
        elif t1_hit:
            final_outcome = "T1_HIT"
            r_multiple    = round(rr_t1, 2)
            break
        elif stop_hit:
            final_outcome = "STOP_HIT"
            r_multiple    = -1.0
            break

    if final_outcome == "OPEN":
        return {
            "mfe_pct":       mfe_pct,
            "mae_pct":       mae_pct,
            "final_outcome": "OPEN",
            "r_multiple":    "",
            "resolved":      "False",
        }

    return {
        "mfe_pct":       mfe_pct,
        "mae_pct":       mae_pct,
        "final_outcome": final_outcome,
        "r_multiple":    r_multiple,
        "resolved":      "True",
    }


# ═══════════════════════════════════════════════════════
# STAGE 2 — RESOLVE OUTCOMES
# ═══════════════════════════════════════════════════════

def resolve_opportunity_outcomes(
    journal_dir:  str  = "journal",
    force_recheck: bool = False,
    expiry_days:  int  = DEFAULT_EXPIRY_DAYS,
) -> dict:
    """
    Resolves post-scan price outcomes for all unique setups.

    Algorithm:
      1. Load scan_log.csv, replay into in-memory SetupStore
         → one candidate per unique (strategy, ticker) setup
      2. Also run expiry: setups older than expiry_days are marked EXPIRED
         (affects the live SetupStore, not the replay)
      3. Load existing opportunity_log.csv (skip already-resolved setup_ids)
      4. Fetch yfinance OHLCV and resolve bar-by-bar for unresolved setups
      5. Merge with prior results and write atomically

    Args:
        journal_dir   : path to journal folder (default: "journal")
        force_recheck : if True, re-resolve all setups including already-resolved
        expiry_days   : setups older than this many trading days are expired

    Returns:
        dict with counts: resolved, still_open, errors, skipped
    """
    log_path = get_opportunity_log_path(journal_dir)
    cleanup_orphaned_temp(log_path)

    # Also expire stale setups in the live store
    from datetime import date as _date
    today_str = _date.today().isoformat()
    live_store = SetupStore(journal_dir)
    expired = live_store.expire_stale_setups(today_str, expiry_days=expiry_days)
    if expired:
        live_store.save()
        print(f"  Expired {len(expired)} stale setup(s) (>{expiry_days} trading days).")

    scan_df = load_scan_log(journal_dir)
    if scan_df.empty:
        print("  No scan_log data — run --today first.")
        return {"resolved": 0, "still_open": 0, "errors": 0, "skipped": 0}

    candidates = _build_candidate_index(scan_df)
    if candidates.empty:
        print("  No candidates with valid trade plans in scan_log.")
        return {"resolved": 0, "still_open": 0, "errors": 0, "skipped": 0}

    # Load existing outcomes to skip already-resolved setup_ids
    resolved_ids: set = set()
    existing_resolved = pd.DataFrame(columns=OPP_COLUMNS)

    if log_path.exists() and log_path.stat().st_size > 0:
        try:
            existing = pd.read_csv(log_path)
            for col in OPP_COLUMNS:
                if col not in existing.columns:
                    existing[col] = ""
            # Backward compat: old logs use (ticker, scan_date) as key
            if "setup_id" not in existing.columns or existing["setup_id"].isna().all():
                existing["setup_id"] = existing["ticker"] + "|" + existing.get("scan_date", "").astype(str)
            existing = existing[OPP_COLUMNS].copy()

            if not force_recheck:
                resolved_ids = set(
                    existing.loc[existing["resolved"].astype(str) == "True", "setup_id"]
                )
            existing_resolved = existing[existing["setup_id"].isin(resolved_ids)].copy()
        except Exception:
            pass

    to_resolve = candidates[~candidates["setup_id"].isin(resolved_ids)].copy()

    print(f"\n  Unique setups: {len(candidates)} total | "
          f"{len(resolved_ids)} already resolved | "
          f"{len(to_resolve)} to process now")

    resolved_count = 0
    still_open     = 0
    errors         = 0
    result_rows    = []

    for _, row in to_resolve.iterrows():
        ticker    = row["ticker"]
        first_seen = row["first_seen"]
        status    = row["oc_status"]
        sid       = row["setup_id"]
        label     = f"{first_seen}  {ticker:<16} [{status:<8}] (setup {sid[:30]})"
        print(f"    {label}", end=" ", flush=True)

        try:
            outcome = _resolve_candidate(row)
            updated = {k: row.get(k, "") for k in OPP_COLUMNS}

            if not outcome:
                print("⚠  no price data")
                errors += 1
            elif outcome.get("resolved") == "True":
                updated.update(outcome)
                r_str = f"{outcome.get('r_multiple', '?'):>5}"
                print(f"✓  {outcome['final_outcome']:<9} R={r_str}")
                resolved_count += 1
            else:
                updated.update(outcome)
                mfe = outcome.get("mfe_pct", "?")
                mae = outcome.get("mae_pct", "?")
                print(f"⏳ OPEN  MFE:{mfe:>6}%  MAE:{mae:>6}%")
                still_open += 1

            result_rows.append(updated)

        except Exception as e:
            print(f"✗  error: {e}")
            errors += 1
            result_rows.append({k: row.get(k, "") for k in OPP_COLUMNS})

    # Merge fresh results with prior resolved rows
    fresh_df = (pd.DataFrame(result_rows, columns=OPP_COLUMNS)
                if result_rows else pd.DataFrame(columns=OPP_COLUMNS))
    prior_df = (existing_resolved.drop(columns=["_key"], errors="ignore")
                if not existing_resolved.empty else pd.DataFrame(columns=OPP_COLUMNS))

    merged = pd.concat([prior_df, fresh_df], ignore_index=True)

    if not merged.empty:
        merged["_rank"] = merged["resolved"].apply(lambda x: 1 if str(x) == "True" else 0)
        merged = (merged
                  .sort_values("_rank", ascending=False)
                  .drop_duplicates(subset=["setup_id"], keep="first")
                  .drop(columns=["_rank"])
                  .reset_index(drop=True))

    atomic_csv_write(log_path, merged)

    print(f"\n  Resolution complete: {resolved_count} resolved | "
          f"{still_open} open | {errors} errors | "
          f"{len(resolved_ids)} skipped")
    return {
        "resolved":   resolved_count,
        "still_open": still_open,
        "errors":     errors,
        "skipped":    len(resolved_ids),
    }


# ═══════════════════════════════════════════════════════
# LOAD
# ═══════════════════════════════════════════════════════

def load_opportunity_log(journal_dir: str = "journal") -> pd.DataFrame:
    """
    Loads opportunity_log.csv with correct type coercions.
    Returns empty DataFrame with correct schema if file absent or empty.
    """
    path = get_opportunity_log_path(journal_dir)

    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame(columns=OPP_COLUMNS)

    try:
        df = pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame(columns=OPP_COLUMNS)

    # Ensure all columns exist (backward compat with old schema)
    for col in OPP_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    for col in ["score", "entry_price", "stop_price", "t1", "t2",
                "rr_t1", "mfe_pct", "mae_pct", "r_multiple", "days_active"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df[OPP_COLUMNS]


# ═══════════════════════════════════════════════════════
# ANALYTICS — counts unique setups, not observations
# ═══════════════════════════════════════════════════════

def compute_opportunity_stats(df: pd.DataFrame) -> dict:
    """
    Computes PF, expectancy, win rate per oc_status bucket.

    Each row in df represents a UNIQUE setup (not a daily observation).

    Input:
        df — DataFrame from load_opportunity_log()

    Returns:
        dict keyed by "READY", "WATCH", "REJECTED", "ALL".
        Each value is a stats dict, or None if sample too small (< MIN_SAMPLE).
    """
    resolved = df[
        df["r_multiple"].notna() &
        (df["resolved"].astype(str) == "True")
    ].copy()

    results = {}

    for bucket in ("READY", "WATCH", "REJECTED", "ALL"):
        subset = resolved if bucket == "ALL" else resolved[resolved["oc_status"] == bucket]
        n = len(subset)

        if n < MIN_SAMPLE:
            results[bucket] = None
            continue

        wins   = subset[subset["r_multiple"] > 0]
        losses = subset[subset["r_multiple"] <= 0]

        gross_profit  = float(wins["r_multiple"].sum())        if len(wins)   > 0 else 0.0
        gross_loss    = abs(float(losses["r_multiple"].sum())) if len(losses) > 0 else 0.0
        profit_factor = (gross_profit / gross_loss
                         if gross_loss > 0
                         else (float("inf") if gross_profit > 0 else 0.0))

        avg_mfe = (float(subset["mfe_pct"].mean())
                   if "mfe_pct" in subset and subset["mfe_pct"].notna().any()
                   else None)
        avg_mae = (float(subset["mae_pct"].mean())
                   if "mae_pct" in subset and subset["mae_pct"].notna().any()
                   else None)

        results[bucket] = {
            "n":             n,
            "n_wins":        len(wins),
            "n_losses":      len(losses),
            "win_rate_pct":  round(len(wins) / n * 100, 1),
            "avg_win_r":     round(float(wins["r_multiple"].mean())   if len(wins)   > 0 else 0.0, 3),
            "avg_loss_r":    round(float(losses["r_multiple"].mean()) if len(losses) > 0 else 0.0, 3),
            "expectancy_r":  round(float(subset["r_multiple"].mean()), 3),
            "profit_factor": round(profit_factor, 2),
            "total_r":       round(float(subset["r_multiple"].sum()), 2),
            "avg_mfe":       round(avg_mfe, 2) if avg_mfe is not None else None,
            "avg_mae":       round(avg_mae, 2) if avg_mae is not None else None,
        }

    return results


def get_opportunity_summary(journal_dir: str = "journal") -> dict:
    """Quick health-check: how many unique setups, how many resolved."""
    df = load_opportunity_log(journal_dir)
    if df.empty:
        return {"total": 0, "resolved": 0, "open": 0, "by_status": {}}

    resolved_mask = df["resolved"].astype(str) == "True"
    by_status = df.groupby("oc_status").size().to_dict()

    return {
        "total":     len(df),
        "resolved":  int(resolved_mask.sum()),
        "open":      int((~resolved_mask).sum()),
        "by_status": by_status,
    }
