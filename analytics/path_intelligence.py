"""
analytics/path_intelligence.py — Price-Path Intelligence Engine (Phase NEXT)
============================================================================
Replaces the geometric, validated-non-predictive RR number with EVIDENCE: for a
candidate's cohort, what did history's analogs actually DO — probability of
reaching +5/+10/+15/+20% before a stop, expected value, median hold, MFE/MAE,
win rate — measured by simulating the real forward price PATH.

Why this is possible now
------------------------
cache/ohlcv/*_5y.csv holds full daily bars for every ticker, so for any historical
(ticker, screen_date) we can walk the actual forward path bar-by-bar and resolve
target-vs-stop. reports/validation/robustness_dataset.csv supplies the point-in-time
features (regime, rs_status, sector) with no look-ahead. We JOIN the two.

Deliberate design decisions (evidence-driven, documented — not defaults)
-----------------------------------------------------------------------
• Analog cohort = regime × RS-leadership × ATR-tercile. We do NOT match on
  breakout/volume: Phase E proved both are net-negative noise; matching on them
  injects noise. ATR is the one OOS-durable factor, so it is a cohort axis.
• Stop = entry − 1.5×ATR (the system's actual stop convention, CONFIG
  stop_atr_multiple). Targets are fixed % so cohorts are comparable.
• Intrabar conservatism: if a bar's HIGH ≥ target AND LOW ≤ stop on the same day,
  the STOP is counted as hit first. No optimistic bias.
• EV rule: hold up to `horizon` (60 td) with the 1.5×ATR stop; realised return =
  exit at stop if hit, else the horizon-end close. EV = mean realised return —
  empirically Σ(prob × outcome), with no arbitrary upside cap.

Honest limitations (stated, never hidden)
------------------------------------------
• Survivorship: forward bars come from today's universe file, so names delisted
  after a screen are absent → outcomes skew mildly optimistic.
• A target+stop touched on the same bar is resolved pessimistically (stop), but
  true intrabar order is unknown.
• OOS calibration (train pre-2025 → test 2025-26) is the deployment GATE. Until it
  passes, EV is descriptive only and must NOT rank/filter/size (same rule as RR).

Run:  python -m analytics.path_intelligence build [sample_n]
      python -m analytics.path_intelligence oos
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_DATASET  = Path("reports/validation/robustness_dataset.csv")
_OUTCOMES = Path("reports/validation/path_outcomes.csv")

TARGETS    = (5, 10, 15, 20)      # % move levels
STOP_MULT  = 1.5                  # × ATR (matches CONFIG stop_atr_multiple)
HORIZON    = 60                   # max trading days held
ATR_LEN    = 14
_OOS_SPLIT = "2025-01-01"         # train < split, test ≥ split


# ─────────────────────────────────────────────────────────────────────────────
# BARS + ATR
# ─────────────────────────────────────────────────────────────────────────────
def _latest_5y_bundle() -> Path:
    files = sorted(glob.glob("cache/ohlcv/*_5y.csv"))
    if not files:
        raise FileNotFoundError("no cache/ohlcv/*_5y.csv bundle found")
    return Path(files[-1])


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int = ATR_LEN) -> np.ndarray:
    """Wilder ATR as a numpy array aligned to the bars (NaN for the warm-up)."""
    prev_close = np.empty_like(close)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = np.full_like(tr, np.nan)
    if len(tr) < n:
        return atr
    atr[n - 1] = tr[:n].mean()
    for i in range(n, len(tr)):
        atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    return atr


def _load_bars(bundle: Path) -> dict[str, dict]:
    """{ticker: {dates, high, low, close, atr}} as numpy arrays, sorted by date."""
    raw = pd.read_csv(bundle, parse_dates=["Date"]).sort_values(["ticker", "Date"])
    out: dict[str, dict] = {}
    for tkr, g in raw.groupby("ticker"):
        h = g["High"].to_numpy(float); l = g["Low"].to_numpy(float)
        c = g["Close"].to_numpy(float)
        out[str(tkr)] = {
            "dates": g["Date"].to_numpy(),
            "high": h, "low": l, "close": c, "atr": _atr(h, l, c),
        }
    return out


# ─────────────────────────────────────────────────────────────────────────────
# SINGLE-TRADE PATH SIMULATION
# ─────────────────────────────────────────────────────────────────────────────
def _simulate(bar: dict, i0: int) -> dict | None:
    """Walk the forward path from entry index i0. Returns target hits, stop, EV
    components, MFE/MAE. None if insufficient data / bad ATR."""
    high, low, close, atr = bar["high"], bar["low"], bar["close"], bar["atr"]
    entry = close[i0]
    a = atr[i0]
    if not np.isfinite(a) or a <= 0 or entry <= 0:
        return None
    stop = entry - STOP_MULT * a
    fh = high[i0 + 1: i0 + 1 + HORIZON]
    fl = low[i0 + 1: i0 + 1 + HORIZON]
    fc = close[i0 + 1: i0 + 1 + HORIZON]
    n = len(fc)
    if n < 5:
        return None

    stop_idx = np.where(fl <= stop)[0]
    stop_day = int(stop_idx[0]) if stop_idx.size else None

    rec: dict = {}
    for X in TARGETS:
        tgt = entry * (1 + X / 100.0)
        t_idx = np.where(fh >= tgt)[0]
        t_day = int(t_idx[0]) if t_idx.size else None
        if t_day is None:
            hit = False
        elif stop_day is None:
            hit = True
        else:
            hit = t_day < stop_day          # tie → stop wins (conservative)
        rec[f"hit_{X}"] = hit
        rec[f"day_{X}"] = (t_day + 1) if hit else np.nan

    # EV rule: stop if hit, else horizon-end close
    if stop_day is not None:
        realized = stop / entry - 1.0
        rec["exit_day"] = stop_day + 1
        rec["reason"] = "stop"
    else:
        realized = fc[-1] / entry - 1.0
        rec["exit_day"] = n
        rec["reason"] = "horizon"
    rec["realized_pct"] = realized * 100.0
    rec["win"] = realized > 0
    rec["mfe_pct"] = (fh.max() / entry - 1.0) * 100.0
    rec["mae_pct"] = (fl.min() / entry - 1.0) * 100.0
    rec["atr_pct"] = a / entry * 100.0
    return rec


# ─────────────────────────────────────────────────────────────────────────────
# BUILD OUTCOMES (join point-in-time features + simulated path)
# ─────────────────────────────────────────────────────────────────────────────
def build_outcomes(sample_n: int = 0, seed: int = 7) -> pd.DataFrame:
    if not _DATASET.exists():
        raise FileNotFoundError(_DATASET)
    ds = pd.read_csv(_DATASET, parse_dates=["screen_date"])
    keep = ["screen_date", "ticker", "regime", "rs_status", "sector"]
    ds = ds[keep].dropna(subset=["screen_date", "ticker"])
    if sample_n and sample_n < len(ds):
        ds = ds.sample(n=sample_n, random_state=seed)

    bars = _load_bars(_latest_5y_bundle())
    recs, skipped = [], 0
    for row in ds.itertuples(index=False):
        bar = bars.get(row.ticker)
        if bar is None:
            skipped += 1
            continue
        i0 = int(np.searchsorted(bar["dates"], np.datetime64(row.screen_date)))
        # require an exact/just-prior bar and room to walk forward
        if i0 >= len(bar["dates"]) or i0 < ATR_LEN:
            skipped += 1
            continue
        sim = _simulate(bar, i0)
        if sim is None:
            skipped += 1
            continue
        sim.update({"screen_date": row.screen_date, "ticker": row.ticker,
                    "regime": row.regime, "rs_status": row.rs_status,
                    "sector": row.sector})
        recs.append(sim)
    out = pd.DataFrame(recs)
    print(f"built {len(out):,} path outcomes ({skipped:,} skipped)")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# COHORTS
# ─────────────────────────────────────────────────────────────────────────────
def _regime_simple(r: str) -> str:
    r = str(r).upper()
    if "BULL" in r:
        return "BULL"
    if "BEAR" in r:
        return "BEAR"
    return "NEUTRAL"


def add_cohort_keys(df: pd.DataFrame, atr_edges=None):
    """Add regime_s, atr_bucket, cohort columns. Returns (df, atr_edges)."""
    df = df.copy()
    df["regime_s"] = df["regime"].map(_regime_simple)
    if atr_edges is None:
        atr_edges = np.array(df["atr_pct"].quantile([0.0, 1/3, 2/3, 1.0]).to_numpy(),
                             dtype=float)
        atr_edges[0], atr_edges[-1] = -np.inf, np.inf
    df["atr_bucket"] = pd.cut(df["atr_pct"], bins=atr_edges,
                              labels=["loATR", "medATR", "hiATR"], include_lowest=True)
    df["cohort"] = (df["regime_s"].astype(str) + "|" + df["rs_status"].astype(str)
                    + "|" + df["atr_bucket"].astype(str))
    return df, atr_edges


def cohort_stats(df: pd.DataFrame, min_n: int = 30) -> pd.DataFrame:
    rows = []
    for key, g in df.groupby("cohort"):
        if len(g) < min_n:
            continue
        rec = {"cohort": key, "n": len(g),
               "ev_pct": round(g["realized_pct"].mean(), 2),
               "win_pct": round(g["win"].mean() * 100, 1),
               "median_hold": int(g["exit_day"].median()),
               "med_mfe": round(g["mfe_pct"].median(), 1),
               "med_mae": round(g["mae_pct"].median(), 1)}
        for X in TARGETS:
            rec[f"p{X}"] = round(g[f"hit_{X}"].mean() * 100, 1)
            d = g.loc[g[f"hit_{X}"], f"day_{X}"]
            rec[f"days{X}"] = int(d.median()) if len(d) else None
        rows.append(rec)
    return pd.DataFrame(rows).sort_values("ev_pct", ascending=False)


# ─────────────────────────────────────────────────────────────────────────────
# OOS CALIBRATION — the deployment gate
# ─────────────────────────────────────────────────────────────────────────────
def oos_calibration(df: pd.DataFrame) -> None:
    df, edges = add_cohort_keys(df)
    train = df[df["screen_date"] < _OOS_SPLIT]
    test  = df[df["screen_date"] >= _OOS_SPLIT]
    print(f"\nTrain n={len(train):,} (<{_OOS_SPLIT})   Test n={len(test):,} (≥{_OOS_SPLIT})")
    if len(test) < 200 or len(train) < 1000:
        print("  insufficient data for OOS gate"); return

    # predicted EV per cohort from TRAIN; map onto TEST rows
    tr_ev = train.groupby("cohort")["realized_pct"].mean()
    tr_n  = train.groupby("cohort")["realized_pct"].count()
    valid = tr_n[tr_n >= 30].index
    t = test[test["cohort"].isin(valid)].copy()
    t["pred_ev"] = t["cohort"].map(tr_ev)
    t = t.dropna(subset=["pred_ev", "realized_pct"])
    print(f"  test rows with a trained cohort: {len(t):,}")
    if len(t) < 200:
        print("  too few test rows after cohort match — INCONCLUSIVE"); return

    # 1) rank correlation: does predicted EV sort realised returns OOS?
    rho = t["pred_ev"].rank().corr(t["realized_pct"].rank())
    pear = t["pred_ev"].corr(t["realized_pct"])
    print(f"\n  corr(pred_EV, realised)  Spearman {rho:+.3f}   Pearson {pear:+.3f}")

    # 2) decile lift: top-quintile predicted vs bottom-quintile realised
    t["pq"] = pd.qcut(t["pred_ev"], 5, labels=False, duplicates="drop")
    by = t.groupby("pq")["realized_pct"].agg(["mean", "count"])
    print("\n  predicted-EV quintile → realised mean return")
    for q, r in by.iterrows():
        print(f"    Q{int(q)+1}  pred-bucket  realised {r['mean']:+.2f}%  (n={int(r['count'])})")
    lift = by["mean"].iloc[-1] - by["mean"].iloc[0] if len(by) >= 2 else float("nan")

    print("\n" + "─" * 64)
    if rho >= 0.05 and lift > 0.5:
        verdict = (f"PASSES OOS GATE — predicted EV sorts future returns "
                   f"(Spearman {rho:+.3f}, top−bottom lift {lift:+.2f}%). "
                   f"Safe to rank/size by EV.")
    elif rho >= 0.03:
        verdict = (f"MARGINAL — weak OOS signal (Spearman {rho:+.3f}, lift {lift:+.2f}%). "
                   f"Show EV as evidence; do NOT rank/size by it yet.")
    else:
        verdict = (f"FAILS OOS GATE — predicted EV does NOT sort future returns "
                   f"(Spearman {rho:+.3f}, lift {lift:+.2f}%). EV is descriptive only; "
                   f"do NOT use for ranking/sizing. Same disposition as RR.")
    print("VERDICT:", verdict)
    print("─" * 64)


# ─────────────────────────────────────────────────────────────────────────────
# COHORT CACHE — for the live screen (descriptive evidence, NOT a ranker)
# ─────────────────────────────────────────────────────────────────────────────
_COHORT_CACHE = Path("reports/validation/path_cohorts.json")


def build_cohort_cache(out: Path = _COHORT_CACHE) -> dict:
    """Aggregate outcomes into per-cohort stats + the OOS verdict, cache to JSON so
    the cockpit can show analog probabilities cheaply. EV/probabilities are flagged
    descriptive_only=True because they FAILED the OOS ranking gate."""
    import json
    df = _load_or_build()
    dfc, edges = add_cohort_keys(df)
    cs = cohort_stats(dfc).set_index("cohort")

    # OOS spearman (recompute quietly for the honesty flag)
    d2, _ = add_cohort_keys(df)
    tr = d2[d2["screen_date"] < _OOS_SPLIT]; te = d2[d2["screen_date"] >= _OOS_SPLIT]
    tr_ev = tr.groupby("cohort")["realized_pct"].mean()
    tr_n = tr.groupby("cohort")["realized_pct"].count()
    te = te[te["cohort"].isin(tr_n[tr_n >= 30].index)].copy()
    te["pred"] = te["cohort"].map(tr_ev)
    te = te.dropna(subset=["pred", "realized_pct"])
    rho = float(te["pred"].rank().corr(te["realized_pct"].rank())) if len(te) > 200 else None

    cache = {
        "n_obs": int(len(df)),
        "atr_inner_edges": [float(edges[1]), float(edges[2])],
        "oos_spearman": rho,
        "oos_gate": "FAILS" if (rho is None or rho < 0.05) else "PASSES",
        "descriptive_only": True,
        "cohorts": {k: {c: (None if pd.isna(v) else v) for c, v in r.items()}
                    for k, r in cs.iterrows()},
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    return cache


def load_cohort_cache(cache: Path = _COHORT_CACHE):
    import json
    try:
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def lookup_cohort(cache: dict, regime: str, rs_status: str, atr_pct: float):
    """Map a live candidate to its analog cohort and return the descriptive stats
    (or None). Falls back to a coarser regime×RS cohort if the ATR-specific one is
    thin/missing — averaging the available ATR buckets."""
    if not cache or atr_pct is None:
        return None
    e1, e2 = cache.get("atr_inner_edges", [0, 0])
    bucket = "loATR" if atr_pct < e1 else ("medATR" if atr_pct < e2 else "hiATR")
    rs = _regime_simple(regime)
    key = f"{rs}|{rs_status}|{bucket}"
    cohorts = cache.get("cohorts", {})
    if key in cohorts:
        return cohorts[key]
    # coarser fallback: average across ATR buckets for this regime×RS
    sibs = [v for k, v in cohorts.items() if k.startswith(f"{rs}|{rs_status}|")]
    if not sibs:
        return None
    tot = sum(s["n"] for s in sibs)
    agg = {"n": tot, "approx": True}
    for f in ("ev_pct", "win_pct", "p10", "p15", "p20", "med_mae", "median_hold"):
        agg[f] = round(sum(s[f] * s["n"] for s in sibs if s.get(f) is not None) / tot, 1)
    return agg


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def _load_or_build(sample_n: int = 0) -> pd.DataFrame:
    if _OUTCOMES.exists():
        return pd.read_csv(_OUTCOMES, parse_dates=["screen_date"])
    df = build_outcomes(sample_n)
    _OUTCOMES.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(_OUTCOMES, index=False, encoding="utf-8")
    return df


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else "build"
    if cmd == "build":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 0
        df = build_outcomes(n)
        _OUTCOMES.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(_OUTCOMES, index=False, encoding="utf-8")
        print(f"cached → {_OUTCOMES}")
        dfc, _ = add_cohort_keys(df)
        cs = cohort_stats(dfc)
        print(f"\nTop cohorts by EV (n≥30):")
        cols = ["cohort", "n", "ev_pct", "win_pct", "median_hold",
                "p10", "p15", "p20", "med_mae"]
        print(cs[cols].head(12).to_string(index=False))
    elif cmd == "oos":
        oos_calibration(_load_or_build())
    elif cmd == "cache":
        c = build_cohort_cache()
        print(f"cached {len(c['cohorts'])} cohorts → {_COHORT_CACHE}  "
              f"(OOS {c['oos_gate']}, spearman {c['oos_spearman']})")
