"""
analytics/target_calibration.py — Evidence-Based Target & Stop Calibration (Phase NEXT+1)
=========================================================================================
Replaces GEOMETRIC targets (prior swing high / measured move — which land in the
top-10% outlier zone and rarely fill) with targets & stops derived from the actual
historical EXCURSION distribution, expressed in ATR units so they self-scale to a
name's volatility.

Design (how a systematic swing desk actually does it)
-----------------------------------------------------
For 94k point-in-time analogs we walk the real forward path (≤60 td) and record,
in ATR units relative to entry:
  • mfe_R          — max favourable excursion (how far it ran up)
  • mae_R          — max adverse excursion (full-window)
  • mae_to_peak_R  — heat endured BEFORE the favourable peak (the heat winners take)
  • first-passage day for a GRID of target & stop ATR-multiples (for P(target<stop))

Then:
  • STOP k_stop  = beyond the heat that real-move winners survive (≈15th pct of
                   mae_to_peak_R among trades with mfe_R ≥ 2) → keeps winners in.
  • T1   k_t1    = largest target multiple still reached (before that stop) by
                   ≥ P1 (default 0.60) of analogs — achievable / bankable.
  • T2   k_t2    = largest reached by ≥ P2 (default 0.33) — the runner.

OOS GATE (calibration stability, NOT ranking):
  Calibrate on train (<2025), then measure on test (≥2025) the ACTUAL fill rate of
  T1/T2 and the stop-out rate. If train≈test (|Δ| small), the calibration is
  trustworthy. (This is a distributional-stability question and can pass even though
  the EV/return-RANKING gate failed — see analytics/path_intelligence.)

Run:  python -m analytics.target_calibration            (calibrate + OOS report)
      python -m analytics.target_calibration cache       (write calibrated_targets.json)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from analytics.path_intelligence import _load_bars, _latest_5y_bundle, ATR_LEN, _regime_simple

_DATASET = Path("reports/validation/robustness_dataset.csv")
_CACHE   = Path("reports/validation/calibrated_targets.json")

HORIZON     = 60
_OOS_SPLIT  = "2025-01-01"
TARGET_GRID = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0]   # ATR multiples
STOP_GRID   = [1.0, 1.5, 2.0, 2.5, 3.0]
# Aim T1/T2 at a TARGET fill PROBABILITY (closest grid point), not the extreme — this
# keeps targets out of the unstable fat-tail and makes them achievable/trustworthy.
P1_AIM, P2_AIM = 0.65, 0.45      # T1 ≈ 65% reach, T2 ≈ 45% reach (in-sample)
T2_CAP_R    = 3.5                # never put the runner past 3.5R (tail = unstable OOS)
WINNER_MFE_R = 2.0               # "a real move" threshold for stop calibration
STOP_SURVIVE_PCT = 15            # keep ~85% of real-move winners un-stopped


def _simulate_row(bar: dict, i0: int) -> dict | None:
    high, low, close, atr = bar["high"], bar["low"], bar["close"], bar["atr"]
    entry, a = close[i0], atr[i0]
    if not np.isfinite(a) or a <= 0 or entry <= 0:
        return None
    fh = high[i0 + 1: i0 + 1 + HORIZON]
    fl = low[i0 + 1: i0 + 1 + HORIZON]
    if len(fh) < 5:
        return None
    up = (fh - entry) / a            # highs in R
    dn = (fl - entry) / a            # lows in R
    peak_idx = int(np.argmax(up))
    rec = {
        "mfe_R": float(up.max()),
        "mae_R": float(dn.min()),
        "mae_to_peak_R": float(dn[:peak_idx + 1].min()),
    }
    for kt in TARGET_GRID:
        hit = np.where(up >= kt)[0]
        rec[f"t_{kt}"] = int(hit[0]) if hit.size else np.nan
    for ks in STOP_GRID:
        hit = np.where(dn <= -ks)[0]
        rec[f"s_{ks}"] = int(hit[0]) if hit.size else np.nan
    return rec


def build(sample_n: int = 0, seed: int = 7) -> pd.DataFrame:
    ds = pd.read_csv(_DATASET, parse_dates=["screen_date"])[
        ["screen_date", "ticker", "regime", "rs_status"]].dropna()
    if sample_n and sample_n < len(ds):
        ds = ds.sample(n=sample_n, random_state=seed)
    bars = _load_bars(_latest_5y_bundle())
    recs, skip = [], 0
    for row in ds.itertuples(index=False):
        bar = bars.get(row.ticker)
        if bar is None:
            skip += 1; continue
        i0 = int(np.searchsorted(bar["dates"], np.datetime64(row.screen_date)))
        if i0 >= len(bar["dates"]) or i0 < ATR_LEN:
            skip += 1; continue
        sim = _simulate_row(bar, i0)
        if sim is None:
            skip += 1; continue
        sim.update({"screen_date": row.screen_date, "regime": row.regime,
                    "rs_status": row.rs_status})
        recs.append(sim)
    print(f"simulated {len(recs):,} analogs ({skip:,} skipped)")
    return pd.DataFrame(recs)


def _nearest(grid, val):
    return min(grid, key=lambda g: abs(g - val))


def calibrate(train: pd.DataFrame) -> dict:
    # 1) STOP from winner heat
    movers = train[train["mfe_R"] >= WINNER_MFE_R]
    heat = movers["mae_to_peak_R"]                       # negative R
    k_stop_raw = -np.percentile(heat, STOP_SURVIVE_PCT)  # flip sign → positive R
    k_stop = _nearest(STOP_GRID, k_stop_raw)

    # 2) P(reach target before stop) for each target multiple, given k_stop
    s_day = train[f"s_{k_stop}"]
    reach = {}
    for kt in TARGET_GRID:
        t_day = train[f"t_{kt}"]
        before = (t_day.notna()) & ((s_day.isna()) | (t_day < s_day))
        reach[kt] = float(before.mean())

    # Pick the grid point whose reach is CLOSEST to the aim (not the extreme tail).
    k_t1 = min(TARGET_GRID, key=lambda kt: abs(reach[kt] - P1_AIM))
    cands = [kt for kt in TARGET_GRID if kt > k_t1 and kt <= T2_CAP_R]
    k_t2 = (min(cands, key=lambda kt: abs(reach[kt] - P2_AIM))
            if cands else _nearest(TARGET_GRID, min(k_t1 + 1.0, T2_CAP_R)))
    return {"k_stop": k_stop, "k_t1": k_t1, "k_t2": k_t2,
            "p_t1_train": round(reach[k_t1], 3), "p_t2_train": round(reach.get(k_t2, 0), 3),
            "k_stop_raw": round(float(k_stop_raw), 2),
            "reach_train": {k: round(v, 3) for k, v in reach.items()}}


def _measure(df: pd.DataFrame, lv: dict) -> dict:
    s_day = df[f"s_{lv['k_stop']}"]
    out = {}
    for tag, k in (("t1", lv["k_t1"]), ("t2", lv["k_t2"])):
        t_day = df[f"t_{k}"]
        before = (t_day.notna()) & ((s_day.isna()) | (t_day < s_day))
        out[f"p_{tag}"] = round(float(before.mean()), 3)
        d = df.loc[before, f"t_{k}"]
        out[f"days_{tag}"] = int(d.median() + 1) if len(d) else None
    out["stop_rate"] = round(float(s_day.notna().mean()), 3)
    return out


def run_oos(df: pd.DataFrame) -> dict:
    train = df[df["screen_date"] < _OOS_SPLIT]
    test  = df[df["screen_date"] >= _OOS_SPLIT]
    lv = calibrate(train)
    tr = _measure(train, lv)
    te = _measure(test, lv)
    print(f"\nCalibrated on train (n={len(train):,}):  "
          f"stop {lv['k_stop']}R · T1 {lv['k_t1']}R · T2 {lv['k_t2']}R")
    print(f"\n{'':12s}{'train':>10}{'test (OOS)':>12}{'Δ':>8}")
    rows = [("P(reach T1)", tr["p_t1"], te["p_t1"]),
            ("P(reach T2)", tr["p_t2"], te["p_t2"]),
            ("stop-out rate", tr["stop_rate"], te["stop_rate"])]
    max_d = 0.0
    for name, a, b in rows:
        d = abs(a - b); max_d = max(max_d, d)
        print(f"{name:12s}{a:>10.1%}{b:>12.1%}{d:>+8.1%}")
    print(f"\nMedian days → T1 {te['days_t1']}  ·  T2 {te['days_t2']}  (OOS)")
    print("─" * 50)
    if max_d <= 0.07:
        verdict = (f"CALIBRATION STABLE OOS (max Δ {max_d:.1%}) — fill rates hold "
                   f"out-of-sample. TRUSTWORTHY: deploy these levels.")
    elif max_d <= 0.12:
        verdict = (f"MOSTLY STABLE (max Δ {max_d:.1%}) — usable; widen T1 confidence band.")
    else:
        verdict = (f"UNSTABLE (max Δ {max_d:.1%}) — fill rates drift OOS; treat as "
                   f"approximate, do not promise the probabilities.")
    print("VERDICT:", verdict)
    print("─" * 50)
    lv.update({"p_t1_test": te["p_t1"], "p_t2_test": te["p_t2"],
               "stop_rate_test": te["stop_rate"], "days_t1": te["days_t1"],
               "days_t2": te["days_t2"], "max_oos_drift": round(max_d, 3),
               "stable": max_d <= 0.12})
    return lv


def apply_levels(entry: float, atr: float, lv: dict) -> dict:
    """Live: convert calibrated ATR-multiples → ₹ stop / T1 / T2 + their fill rates."""
    if not lv or not atr or atr <= 0 or not entry:
        return {}
    stop = entry - lv["k_stop"] * atr
    t1   = entry + lv["k_t1"] * atr
    t2   = entry + lv["k_t2"] * atr
    return {
        "stop": round(stop, 2), "t1": round(t1, 2), "t2": round(t2, 2),
        "stop_pct": round((stop / entry - 1) * 100, 1),
        "t1_pct": round((t1 / entry - 1) * 100, 1),
        "t2_pct": round((t2 / entry - 1) * 100, 1),
        "p_t1": lv.get("p_t1_test", lv.get("p_t1_train")),
        "p_t2": lv.get("p_t2_test", lv.get("p_t2_train")),
        "days_t1": lv.get("days_t1"), "days_t2": lv.get("days_t2"),
        "stop_rate": lv.get("stop_rate_test"),
    }


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else "oos"
    df = build(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
    lv = run_oos(df)
    if cmd == "cache":
        _CACHE.write_text(json.dumps(lv, indent=2), encoding="utf-8")
        print(f"\ncached calibrated levels → {_CACHE}")
