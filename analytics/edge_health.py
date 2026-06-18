"""
analytics/edge_health.py — Walk-Forward Edge Health (is the edge alive, dying, dead?)
=====================================================================================
Averages hide decay. This computes the proven signal's strength YEAR BY YEAR plus a
recent rolling window, so the trader sees whether the leader edge is still breathing
— and *why* it weakened (regime composition) — instead of trusting a 5-year mean.

Metric: MARKET_LEADER 20d-forward return minus the universe (leader excess), per
calendar year, from reports/validation/robustness_dataset.csv (94,637 point-in-time
obs). Also: BULL-regime share per year (the dominant driver of decay) and the most
recent ~8-week rolling excess (current expectancy).

Cached to reports/validation/edge_health.json (rebuilds when the dataset changes).
Pure measurement — no scores touched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

_DATASET = Path("reports/validation/robustness_dataset.csv")
_CACHE   = Path("reports/validation/edge_health.json")


def compute(dataset: Path = _DATASET) -> dict:
    df = pd.read_csv(dataset, parse_dates=["screen_date"])
    df["year"] = df["screen_date"].dt.year
    uni_by_year = df.groupby("year")["fwd20"].mean()

    years = {}
    for y, g in df.groupby("year"):
        leaders = g[g["rs_status"] == "MARKET_LEADER"]["fwd20"]
        if len(leaders) < 30:
            continue
        bull = g["regime"].astype(str).str.contains("BULL").mean()
        years[int(y)] = {
            "leader_ret": round(float(leaders.mean()), 2),
            "uni_ret": round(float(uni_by_year[y]), 2),
            "excess": round(float(leaders.mean() - uni_by_year[y]), 2),
            "leader_win": round(float((leaders > 0).mean() * 100), 1),
            "bull_share": round(float(bull), 2),
            "n_leaders": int(len(leaders)),
        }

    # recent rolling: last 8 calendar weeks of screens
    cutoff = df["screen_date"].max() - pd.Timedelta(weeks=8)
    recent = df[df["screen_date"] >= cutoff]
    rl = recent[recent["rs_status"] == "MARKET_LEADER"]["fwd20"]
    recent_excess = (round(float(rl.mean() - recent["fwd20"].mean()), 2)
                     if len(rl) >= 20 else None)

    # decay drivers (early vs late bull share / mean trend proxy via excess)
    yrs_sorted = sorted(years)
    drivers = {}
    if len(yrs_sorted) >= 2:
        drivers = {"bull_share_first": years[yrs_sorted[0]]["bull_share"],
                   "bull_share_last": years[yrs_sorted[-1]]["bull_share"]}

    # verdict
    last_excess = years[yrs_sorted[-1]]["excess"] if yrs_sorted else 0.0
    positives = sum(1 for y in years.values() if y["excess"] > 0)
    if last_excess > 0.3 and (recent_excess or 0) > 0:
        verdict = "ALIVE (weak) — recent excess positive; deploy small, keep watching"
    elif last_excess <= 0 and (recent_excess or 0) <= 0:
        verdict = "DYING/DORMANT — recent excess ≤0; edge not currently paying. Stand small or aside"
    else:
        verdict = "MIXED — excess flickering near zero; treat as risk overlay, not alpha"

    return {
        "years": years, "recent_excess": recent_excess,
        "drivers": drivers, "verdict": verdict,
        "positive_years": positives, "total_years": len(years),
        "date_max": str(df["screen_date"].max())[:10],
    }


def build_and_cache(dataset: Path = _DATASET, cache: Path = _CACHE) -> dict:
    h = compute(dataset)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(h, indent=2), encoding="utf-8")
    return h


def load(cache: Path = _CACHE, dataset: Path = _DATASET):
    try:
        if cache.exists():
            fresh = (not dataset.exists()
                     or cache.stat().st_mtime >= dataset.stat().st_mtime)
            if fresh:
                return json.loads(cache.read_text(encoding="utf-8"))
        if dataset.exists():
            return build_and_cache(dataset, cache)
    except Exception:
        pass
    return None


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    h = build_and_cache()
    print("Leader 20d excess vs universe, by year:")
    for y in sorted(h["years"]):
        v = h["years"][y]
        print(f"  {y}: excess {v['excess']:+.2f}%  (leader {v['leader_ret']:+.2f}% "
              f"vs uni {v['uni_ret']:+.2f}%, win {v['leader_win']}%, "
              f"BULL share {v['bull_share']})")
    print(f"\nRecent 8wk excess: {h['recent_excess']}")
    print(f"Drivers: {h['drivers']}")
    print(f"VERDICT: {h['verdict']}")
