"""
verification/phase4.py — Phase 4 (Composite Ranking Engine) acceptance checks
=============================================================================
Proves the Phase-4 contract (must pass 8/8) and prints the sample output:
Top 20, grade distribution, component breakdowns, drivers, warnings.

  1. All surviving stocks receive scores.
  2. All scores bounded 0..100.
  3. Ranks contiguous & unique.
  4. Deterministic across repeated runs.
  5. Explanation quality — drivers never empty; full component breakdown present.
  6. Leadership validation — ≥80% of A/A+ names come from the TOP_20 RS bucket.
  7. Component contributions sum correctly to the final score.
  8. Hard rejects (price<50, vol<500k, mcap<2000Cr) never appear in rankings.

Usage:
    python verification/phase4.py
    python verification/phase4.py --sample 200
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from scanner.universe_builder import build_universe                      # noqa: E402
from scanner.data_feed import get_ohlcv                                  # noqa: E402
from scanner.sector_engine import SectorRanker                           # noqa: E402
from scanner.relative_strength_engine import (                          # noqa: E402
    RelativeStrengthRanker, fetch_nifty)
from scanner.composite_engine import (                                  # noqa: E402
    CompositeRanker, WEIGHTS, _HISTORY_DIR, _HISTORY_CSV)

logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")
GREEN, RED, DIM, BOLD, YEL, RST = (
    "\033[92m", "\033[91m", "\033[2m", "\033[1m", "\033[93m", "\033[0m")


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))
    return ok


def _synthetic_df(price: float, volume: float, bars: int = 160) -> pd.DataFrame:
    idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=bars, freq="B")
    c = pd.Series([price] * bars, index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99,
                         "Close": c, "Volume": [volume] * bars}, index=idx)


def _sample_output(snap) -> None:
    print(f"\n  {BOLD}══ TOP 20 SWING CANDIDATES ══{RST}")
    print(f"  {BOLD}{'#':>3}  {'SYMBOL':13s}  {'SCORE':>5}  {'GR':>3}  "
          f"{'SECTOR':22s}  {'RS':14s}  DRIVERS{RST}")
    print(f"  {'─'*108}")
    for r in snap.top_n(20):
        gc = GREEN if r.grade in ("A+", "A") else (YEL if r.grade in ("B+", "B") else "")
        drv = " · ".join(r.drivers[:3])
        print(f"  {r.rank:>3}  {r.symbol[:13]:13s}  {gc}{r.score:>5.1f}{RST}  "
              f"{gc}{r.grade:>3}{RST}  {r.sector[:22]:22s}  {r.rs_status[:14]:14s}  {drv}")

    print(f"\n  {BOLD}══ GRADE DISTRIBUTION ══{RST}")
    gd = snap.grade_distribution()
    print("  " + "   ".join(f"{g}:{n}" for g, n in gd.items())
          + f"    (ranked {len(snap.rows)}, rejected {len(snap.rejected)})")

    print(f"\n  {BOLD}══ SECTOR DISTRIBUTION (top 8) ══{RST}")
    sd = list(snap.sector_distribution().items())[:8]
    print("  " + "   ".join(f"{s}:{n}" for s, n in sd))

    print(f"\n  {BOLD}══ COMPONENT BREAKDOWN — top 3 ══{RST}")
    for r in snap.top_n(3):
        comp = "  ".join(f"{k}={v:g}" for k, v in r.components.items())
        print(f"\n  {BOLD}{r.symbol}{RST}  score={r.score} grade={r.grade}")
        print(f"    components: {comp}")
        print(f"    drivers:    {' · '.join(r.drivers)}")
        print(f"    warnings:   {(' · '.join(r.warnings)) if r.warnings else '—'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--period", default="1y")
    args = ap.parse_args()

    print(f"\n  {'='*64}\n  PHASE 4 VERIFICATION — Composite Ranking Engine\n  {'='*64}\n")
    results: list[bool] = []

    uni = build_universe()
    tickers = uni.tickers[: args.sample] if args.sample else uni.tickers
    sub = uni.df[uni.df["ticker"].isin(tickers)]
    sector_map = dict(zip(sub["ticker"], sub["sector"]))

    print(f"  {DIM}Fetching {len(tickers)} tickers + Nifty @ {args.period}...{RST}")
    feed = get_ohlcv(tickers, period=args.period, min_bars=30)
    nifty = fetch_nifty(period=args.period)
    sector_snap = SectorRanker(sector_map, feed.data, uni.source).rank(persist=False)
    rs_snap = RelativeStrengthRanker(feed.data, sector_map, nifty, sector_snap).rank(persist=False)

    ranker = CompositeRanker(feed.data, sector_map, sector_snap, rs_snap)
    snap = ranker.rank(persist=True)
    print(f"  {DIM}ranked {len(snap.rows)}, rejected {len(snap.rejected)}{RST}")

    _sample_output(snap)
    print(f"\n  {'─'*64}\n  CHECKS\n  {'─'*64}")

    # ── 1. All survivors scored ──────────────────────────────────────────────
    results.append(_check("All surviving stocks receive scores",
                          all(isinstance(r.score, (int, float)) for r in snap.rows),
                          f"{len(snap.rows)} scored"))

    # ── 2. Bounded 0..100 ────────────────────────────────────────────────────
    results.append(_check("All scores bounded 0..100",
                          all(0 <= r.score <= 100 for r in snap.rows),
                          f"min={min(r.score for r in snap.rows):.1f} "
                          f"max={max(r.score for r in snap.rows):.1f}"))

    # ── 3. Ranks contiguous & unique ─────────────────────────────────────────
    ranks = sorted(r.rank for r in snap.rows)
    results.append(_check("Ranks contiguous & unique",
                          ranks == list(range(1, len(snap.rows) + 1)),
                          f"1..{len(snap.rows)}"))

    # ── 4. Deterministic ─────────────────────────────────────────────────────
    snap2 = CompositeRanker(feed.data, sector_map, sector_snap, rs_snap).rank(persist=False)
    sig1 = [(r.ticker, r.rank, r.score) for r in snap.rows]
    sig2 = [(r.ticker, r.rank, r.score) for r in snap2.rows]
    results.append(_check("Deterministic across repeated runs", sig1 == sig2))

    # ── 5. Explanation quality ───────────────────────────────────────────────
    comp_keys = set(WEIGHTS)
    expl_ok = all(r.drivers and set(r.components) == comp_keys for r in snap.rows)
    results.append(_check("Explanation quality (drivers non-empty, full components)",
                          expl_ok, f"{len(comp_keys)} components, drivers always present"))

    # ── 6. Leadership validation ─────────────────────────────────────────────
    top_grades = [r for r in snap.rows if r.grade in ("A+", "A")]
    from_top20 = [r for r in top_grades if r.rs_bucket in ("TOP_10", "TOP_20")]
    ratio = (len(from_top20) / len(top_grades)) if top_grades else 1.0
    results.append(_check("≥80% of A/A+ from TOP_20 RS bucket", ratio >= 0.80,
                          f"{len(from_top20)}/{len(top_grades)} = {ratio*100:.0f}%"))

    # ── 7. Component sum correctness ─────────────────────────────────────────
    tw = sum(WEIGHTS.values())
    max_err = 0.0
    for r in snap.rows:
        expected = sum(r.components[k] * WEIGHTS[k] for k in WEIGHTS) / tw
        max_err = max(max_err, abs(expected - r.score))
    results.append(_check("Component contributions sum to final score",
                          max_err < 0.02, f"max error {max_err:.4f}"))

    # ── 8. Hard rejects excluded (synthetic injection) ───────────────────────
    aug = dict(feed.data)
    aug["FAKEPENNY.NS"] = _synthetic_df(10, 5_000_000)     # price < ₹50
    aug["FAKETHIN.NS"]  = _synthetic_df(200, 2_000)        # turnover ₹0.04Cr < ₹1Cr
    aug["FAKESMALL.NS"] = _synthetic_df(500, 3_000_000)    # mcap < ₹2000Cr
    aug_sector = dict(sector_map, **{t: "DIVERSIFIED" for t in
                                     ("FAKEPENNY.NS", "FAKETHIN.NS", "FAKESMALL.NS")})
    aug_snap = CompositeRanker(aug, aug_sector, sector_snap, rs_snap,
                               market_caps={"FAKESMALL.NS": 1000}).rank(persist=False)
    fakes = {"FAKEPENNY.NS", "FAKETHIN.NS", "FAKESMALL.NS"}
    in_ranking = fakes & {r.ticker for r in aug_snap.rows}
    in_rejected = fakes & {d["ticker"] for d in aug_snap.rejected}
    results.append(_check("Hard rejects excluded from rankings",
                          not in_ranking and in_rejected == fakes,
                          f"{len(in_rejected)}/3 rejected, {len(in_ranking)} leaked"))

    # ── Persistence note ─────────────────────────────────────────────────────
    persisted = (_HISTORY_DIR / f"{snap.week_id}.json").exists() and _HISTORY_CSV.exists()
    print(f"\n  {DIM}weekly snapshot persisted: {persisted} "
          f"({_HISTORY_DIR}/{snap.week_id}.json){RST}")

    print(f"\n  {'='*64}")
    passed = sum(results)
    ok = passed == len(results)
    print(f"  {(GREEN if ok else RED)}{passed}/{len(results)} checks passed{RST}")
    print(f"  {'='*64}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
