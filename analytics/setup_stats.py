"""
analytics/setup_stats.py — Evidence-Based Setup Statistics (Phase M.2)
=====================================================================
Turns the 5-year point-in-time replay into REFERENCE STATISTICS the live screen
can show, so every target / stop / hold number on the cockpit is anchored to
what history actually did — not to a geometric measured-move guess.

Source: reports/validation/robustness_dataset.csv  (218 weekly screens,
94,637 point-in-time observations, 2022-04 → 2026-06, no look-ahead). Each row
carries the screen-time classification + component scores and the realised
forward returns at 5/10/20/60/120 trading days plus mae20 (max adverse
excursion over 20 days).

What this CAN measure honestly (and does):
  • win rate (P[fwd20 > 0]) per setup class / RS bucket
  • realistic expected move: median / mean / p75 of forward returns
  • typical & bad-case drawdown: median & p25 of mae20  → realistic stop distance
  • horizon profile (med10 / med20 / med60)            → natural hold window

What it CANNOT measure from this dataset (stated, never faked):
  • exact T1/T2 *touch* rate or days-to-target/stop — needs the daily price PATH
    or max-favourable-excursion, which the replay did not persist. Reported as
    "needs path replay", not invented.

Output: a compact dict, cached to reports/validation/setup_stats.json so the
cockpit loads it in microseconds instead of re-reading a 14 MB CSV each run.
Pure measurement of an existing dataset — changes no scores, no thresholds.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import pandas as pd

_log = logging.getLogger(__name__)

_DATASET = Path("reports/validation/robustness_dataset.csv")
_CACHE   = Path("reports/validation/setup_stats.json")

# Horizons present in the replay (trading days).
_HORIZONS = ("fwd5", "fwd10", "fwd20", "fwd60", "fwd120")


def _group_stats(g: pd.DataFrame) -> dict:
    """Reference stats for one cohort (a classification or RS bucket)."""
    n = int(len(g))
    if n == 0:
        return {}
    out = {
        "n":          n,
        "win20_pct":  round(float((g["fwd20"] > 0).mean() * 100), 1),
        "med20_pct":  round(float(g["fwd20"].median()), 2),
        "mean20_pct": round(float(g["fwd20"].mean()), 2),
        "p75_20_pct": round(float(g["fwd20"].quantile(0.75)), 2),
        "p90_20_pct": round(float(g["fwd20"].quantile(0.90)), 2),
        # horizon profile → where does the edge flatten? (natural hold)
        "med10_pct":  round(float(g["fwd10"].median()), 2),
        "med60_pct":  round(float(g["fwd60"].median()), 2),
    }
    if "mae20" in g.columns:
        out["med_mae20_pct"] = round(float(g["mae20"].median()), 2)   # typical drawdown
        out["p25_mae20_pct"] = round(float(g["mae20"].quantile(0.25)), 2)  # bad-case
    return out


def compute_setup_stats(dataset: Path = _DATASET) -> dict:
    """Read the replay and compute reference stats by classification and RS bucket."""
    if not dataset.exists():
        raise FileNotFoundError(
            f"{dataset} not found — run the robustness replay first "
            f"(analytics/robustness_audit.py).")
    df = pd.read_csv(dataset)
    needed = {"classification", "rs_status", "fwd10", "fwd20", "fwd60"}
    missing = needed - set(df.columns)
    if missing:
        raise ValueError(f"{dataset} missing columns: {missing}")

    by_class = {str(k): _group_stats(g)
                for k, g in df.groupby("classification")}
    by_rs    = {str(k): _group_stats(g)
                for k, g in df.groupby("rs_status")}

    return {
        "source":      str(dataset),
        "n_obs":       int(len(df)),
        "date_min":    str(df["screen_date"].min()),
        "date_max":    str(df["screen_date"].max()),
        "universe":    _group_stats(df),
        "by_class":    by_class,
        "by_rs":       by_rs,
        # honest scope note carried into any consumer/UI
        "not_measured": "T1/T2 touch-rate and days-to-target/stop need a daily "
                        "price-path replay (max-favourable-excursion) not present "
                        "in this dataset.",
    }


def build_and_cache(dataset: Path = _DATASET, cache: Path = _CACHE) -> dict:
    stats = compute_setup_stats(dataset)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return stats


def load_setup_stats(cache: Path = _CACHE, dataset: Path = _DATASET,
                     auto_build: bool = True) -> Optional[dict]:
    """Load cached stats; (re)build from the dataset if the cache is missing or
    older than the dataset. Returns None if neither is available (screen then
    simply omits the evidence overlay — never crashes)."""
    try:
        if cache.exists():
            fresh = (not dataset.exists()
                     or cache.stat().st_mtime >= dataset.stat().st_mtime)
            if fresh:
                return json.loads(cache.read_text(encoding="utf-8"))
        if auto_build and dataset.exists():
            return build_and_cache(dataset, cache)
    except Exception as exc:
        _log.warning("setup_stats load failed: %s", exc)
    return None


# Map an RS classification string → the by_rs key used in the stats dict.
def expected_move(stats: dict, rs_status: str, classification: str) -> Optional[dict]:
    """Best available reference cohort for a live candidate: prefer its RS bucket
    (the proven signal), fall back to its classification."""
    if not stats:
        return None
    by_rs = stats.get("by_rs", {})
    if rs_status in by_rs:
        return by_rs[rs_status]
    return stats.get("by_class", {}).get(classification)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    s = build_and_cache()
    print(f"Cached {s['n_obs']:,} obs ({s['date_min']} -> {s['date_max']}) -> {_CACHE}")
    print("\nBy classification (20d):")
    for k, v in s["by_class"].items():
        print(f"  {k:12s} n={v['n']:>6}  win={v['win20_pct']:>4}%  "
              f"median={v['med20_pct']:>5}%  p75={v['p75_20_pct']:>5}%  "
              f"MAE={v.get('med_mae20_pct','-')}%")
