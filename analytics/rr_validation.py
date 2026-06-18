"""
analytics/rr_validation.py — Does Risk/Reward actually predict anything? (Phase M)
================================================================================
The screen shows an RR per name = (target − entry) / (entry − stop). The reward
leg is a GEOMETRIC target (prior swing high / measured move). This module tests,
on 5 years of point-in-time data, whether that RR number has ANY relationship to
realised forward returns — i.e. is RR a signal, or decoration?

Method (no look-ahead):
  • For a random sample of (ticker, screen_date) rows from the robustness replay,
    reconstruct the REAL RR using the production EntryContextClassifier +
    RiskRewardScorer on cached OHLCV sliced to screen_date.
  • Join to the realised fwd20 (20-trading-day forward return) and mae20 (max
    adverse excursion) already stored in the replay dataset.
  • Measure: correlation(RR, fwd20); mean/median fwd20 and win-rate by RR bucket;
    correlation(RR, realised risk = mae20).

Verdict logic:
  • PREDICTIVE if fwd20 rises ~monotonically across RR buckets AND |corr| is
    meaningfully > 0.
  • RANDOM if buckets are flat / non-monotonic and corr ≈ 0.

Pure measurement — changes no scores. Run: python -m analytics.rr_validation
"""

from __future__ import annotations

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from scanner.entry_context import EntryContextClassifier
from scanner.actionability_engine import RiskRewardScorer

_DATASET = Path("reports/validation/robustness_dataset.csv")
_RR_BUCKETS = [0, 1, 2, 3, 5, 1e9]
_RR_LABELS  = ["<1", "1–2", "2–3", "3–5", "5+"]


def _latest_5y_bundle() -> Path:
    files = sorted(glob.glob("cache/ohlcv/*_5y.csv"))
    if not files:
        raise FileNotFoundError("no cache/ohlcv/*_5y.csv bundle found")
    return Path(files[-1])


def _load_bars(bundle: Path) -> dict[str, pd.DataFrame]:
    raw = pd.read_csv(bundle, parse_dates=["Date"])
    raw = raw.sort_values(["ticker", "Date"])
    out: dict[str, pd.DataFrame] = {}
    for tkr, g in raw.groupby("ticker"):
        out[str(tkr)] = g.reset_index(drop=True)
    return out


def reconstruct(sample_n: int = 8000, seed: int = 7) -> pd.DataFrame:
    if not _DATASET.exists():
        raise FileNotFoundError(_DATASET)
    ds = pd.read_csv(_DATASET, parse_dates=["screen_date"])
    ds = ds.dropna(subset=["fwd20"])
    # Sample for runtime; this is a precision-of-estimate question, not a census.
    ds = ds.sample(n=min(sample_n, len(ds)), random_state=seed)

    bundle = _latest_5y_bundle()
    bars = _load_bars(bundle)
    classifier = EntryContextClassifier()
    rr_scorer  = RiskRewardScorer()

    recs = []
    skipped = 0
    for row in ds.itertuples(index=False):
        df = bars.get(row.ticker)
        if df is None:
            skipped += 1
            continue
        sub = df[df["Date"] <= row.screen_date]
        if len(sub) < 60:
            skipped += 1
            continue
        sub = sub.reset_index(drop=True)
        try:
            ctx = classifier.classify(sub)
            rr  = rr_scorer.score(sub, ctx)
        except Exception:
            skipped += 1
            continue
        recs.append({
            "rr": rr["rr_ratio"],
            "fwd20": row.fwd20,
            "mae20": getattr(row, "mae20", np.nan),
            "rs_status": row.rs_status,
            "classification": row.classification,
            "context": ctx.context,
        })
    out = pd.DataFrame(recs)
    print(f"reconstructed {len(out):,} rows ({skipped:,} skipped) "
          f"from {bundle.name}")
    return out


def analyse(df: pd.DataFrame) -> None:
    df = df[np.isfinite(df["rr"]) & np.isfinite(df["fwd20"])].copy()
    n = len(df)
    print(f"\nRR distribution: median {df['rr'].median():.2f}  "
          f"mean {df['rr'].mean():.2f}  p90 {df['rr'].quantile(0.9):.2f}")

    # 1) Correlations (Spearman via rank-Pearson — avoids the scipy dependency)
    pear = df["rr"].corr(df["fwd20"])
    spear = df["rr"].rank().corr(df["fwd20"].rank())
    print(f"\ncorr(RR, fwd20):   Pearson {pear:+.3f}   Spearman {spear:+.3f}   (n={n:,})")
    if "mae20" in df and df["mae20"].notna().any():
        pear_r = df["rr"].corr(df["mae20"])
        print(f"corr(RR, mae20):   Pearson {pear_r:+.3f}   "
              f"(does higher RR mean less realised risk? expect <0 if real)")

    # 2) Forward return by RR bucket — the decisive view
    df["bucket"] = pd.cut(df["rr"], bins=_RR_BUCKETS, labels=_RR_LABELS,
                          include_lowest=True, right=False)
    print("\nRR bucket    n      mean fwd20   median   win%")
    means = []
    for lab in _RR_LABELS:
        g = df[df["bucket"] == lab]
        if len(g) == 0:
            continue
        m = g["fwd20"].mean()
        means.append((lab, m))
        print(f"  {lab:<6}  {len(g):>6}   {m:>+8.2f}%   "
              f"{g['fwd20'].median():>+6.2f}%   {(g['fwd20']>0).mean()*100:>4.1f}%")

    # 3) Verdict
    print("\n" + "─" * 60)
    monotonic = all(means[i][1] <= means[i+1][1] + 0.25 for i in range(len(means)-1)) \
        if len(means) >= 3 else False
    strong_corr = abs(spear) >= 0.05
    if monotonic and strong_corr:
        verdict = "PREDICTIVE — higher RR → higher forward return (keep, but recalibrate the target leg to evidence)."
    elif strong_corr:
        verdict = f"WEAK/MIXED — corr {spear:+.3f} is non-zero but buckets are not monotonic; RR carries marginal info at best."
    else:
        verdict = (f"RANDOM — Spearman {spear:+.3f} ≈ 0 and buckets are flat. "
                   f"The RR number does NOT predict forward returns. It should be "
                   f"demoted from any buy/rank decision.")
    print("VERDICT:", verdict)
    print("─" * 60)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    data = reconstruct(sample_n=n)
    analyse(data)
