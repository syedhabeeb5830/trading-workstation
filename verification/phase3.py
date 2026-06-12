"""
verification/phase3.py — Phase 3 (Relative Strength Engine) acceptance checks
=============================================================================
Proves the Phase-3 contract:

  1. Every eligible stock receives an RS score.
  2. Every eligible stock receives an RS rank (contiguous, unique, no dups).
  3. Every stock receives a classification.
  4. Rankings deterministic across runs.
  5. Weekly history snapshot persists (JSON + CSV).
  6. Rank-delta calculations are accurate (synthetic-previous test).
  7. Missing data handled gracefully (ineligible stocks listed, not dropped).
  8. RS buckets (TOP_10/20/50, BOTTOM_50) populated for Phase-4 noise reduction.

Usage:
    python verification/phase3.py
    python verification/phase3.py --sample 150
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from scanner.universe_builder import build_universe                      # noqa: E402
from scanner.data_feed import get_ohlcv                                  # noqa: E402
from scanner.sector_engine import SectorRanker, week_id_for             # noqa: E402
from scanner.relative_strength_engine import (                          # noqa: E402
    RelativeStrengthRanker, RelativeStrengthSnapshot, fetch_nifty,
    _HISTORY_DIR, _HISTORY_CSV,
)

logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")
GREEN, RED, DIM, BOLD, RST = "\033[92m", "\033[91m", "\033[2m", "\033[1m", "\033[0m"


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))
    return ok


def _print_board(snap, n=25) -> None:
    print(f"\n  {BOLD}{'#':>3}  {'SYMBOL':14s}  {'RS':>5}  {'GR':>3}  "
          f"{'STATUS':15s}  {'vNIFTY':>7}  {'vSECT':>7}  {'BUCKET':9s}  Δ{RST}")
    print(f"  {'─'*82}")
    for r in snap.ranked[:n]:
        gc = GREEN if r.grade in ("A+", "A") else ""
        d = f"{r.rank_delta:+d}" if r.rank_delta else "·"
        rn = f"{r.rs_nifty:+.1f}" if r.rs_nifty is not None else "—"
        rs = f"{r.rs_sector:+.1f}" if r.rs_sector is not None else "—"
        print(f"  {r.rank:>3}  {r.symbol[:14]:14s}  {r.score:>5.1f}  "
              f"{gc}{r.grade:>3}{RST}  {r.status:15s}  {rn:>7}  {rs:>7}  {r.bucket:9s}  {d}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--period", default="1y")
    args = ap.parse_args()

    print(f"\n  {'='*64}\n  PHASE 3 VERIFICATION — Relative Strength Engine\n  {'='*64}\n")
    results: list[bool] = []

    uni = build_universe()
    tickers = uni.tickers[: args.sample] if args.sample else uni.tickers
    sub = uni.df[uni.df["ticker"].isin(tickers)]
    sector_map = dict(zip(sub["ticker"], sub["sector"]))

    print(f"  {DIM}Fetching {len(tickers)} tickers + Nifty @ {args.period}...{RST}")
    feed = get_ohlcv(tickers, period=args.period, min_bars=30)
    nifty = fetch_nifty(period=args.period)
    print(f"  {DIM}coverage {feed.coverage*100:.1f}% ({feed.succeeded}/{feed.requested}); "
          f"nifty={'ok' if nifty is not None else 'MISSING'}{RST}")

    sector_snap = SectorRanker(sector_map, feed.data, uni.source).rank(persist=False)
    ranker = RelativeStrengthRanker(feed.data, sector_map, nifty, sector_snap)
    snap = ranker.rank(persist=True)
    _print_board(snap)

    ranked = snap.ranked
    M = len(ranked)

    # ── 1. Every eligible stock has a score ──────────────────────────────────
    results.append(_check("Every eligible stock receives an RS score",
                          all(isinstance(r.score, (int, float)) for r in ranked),
                          f"{M} eligible scored"))

    # ── 2. Ranks contiguous, unique, no dups ─────────────────────────────────
    ranks = sorted(r.rank for r in ranked)
    no_dups = len(ranks) == len(set(ranks))
    contiguous = ranks == list(range(1, M + 1))
    results.append(_check("Ranks contiguous & unique (no dups/gaps)",
                          no_dups and contiguous, f"1..{M}"))

    # ── 3. Every stock classified ────────────────────────────────────────────
    valid_status = {"MARKET_LEADER", "SECTOR_LEADER", "EMERGING_LEADER",
                    "NEUTRAL", "LAGGARD"}
    results.append(_check("Every stock receives a classification",
                          all(r.status in valid_status for r in snap.rows),
                          f"{len(snap.rows)} stocks classified"))

    # ── 4. Deterministic ─────────────────────────────────────────────────────
    snap2 = RelativeStrengthRanker(feed.data, sector_map, nifty, sector_snap).rank(persist=False)
    sig1 = [(r.ticker, r.rank, r.score) for r in snap.ranked]
    sig2 = [(r.ticker, r.rank, r.score) for r in snap2.ranked]
    results.append(_check("Rankings deterministic across runs", sig1 == sig2,
                          "identical order + scores"))

    # ── 5. Snapshot persisted ────────────────────────────────────────────────
    json_path = _HISTORY_DIR / f"{snap.week_id}.json"
    results.append(_check("Weekly snapshot persisted (JSON + CSV)",
                          json_path.exists() and _HISTORY_CSV.exists(),
                          f"{json_path}"))

    # ── 6. Rank-delta accuracy (synthetic previous snapshot) ─────────────────
    prev_week = week_id_for(date.today() - timedelta(days=7))
    prev_path = _HISTORY_DIR / f"{prev_week}.json"
    pre_existing = prev_path.exists()
    backup = prev_path.read_text() if pre_existing else None
    try:
        # Synthetic previous: reverse the current ranks → known deltas.
        prev_rows = []
        for r in ranked:
            pr = copy.deepcopy(r)
            pr.rank = M - r.rank + 1          # reversed
            prev_rows.append(pr)
        prev_snap = RelativeStrengthSnapshot(prev_rows, "2000-01-01T00:00:00",
                                             prev_week, snap.benchmark)
        prev_path.write_text(json.dumps(prev_snap.to_dict(), indent=2))

        snap3 = RelativeStrengthRanker(feed.data, sector_map, nifty,
                                       sector_snap).rank(persist=False)
        s3 = snap3.by_symbol()
        # Expected delta = prev_rank − cur_rank = (M − cur + 1) − cur
        mismatches = 0
        for r in ranked:
            expected = (M - r.rank + 1) - r.rank
            got = s3[r.ticker].rank_delta
            if got != expected:
                mismatches += 1
        results.append(_check("Rank-delta calculations accurate",
                              mismatches == 0,
                              f"0 mismatches across {M} stocks"))
    finally:
        if pre_existing:
            prev_path.write_text(backup)
        elif prev_path.exists():
            prev_path.unlink()

    # ── 7. Missing data graceful ─────────────────────────────────────────────
    ineligible = [r for r in snap.rows if r.rank is None]
    graceful = all(r.status and r.reasons for r in ineligible)
    results.append(_check("Missing data handled gracefully", graceful,
                          f"{len(ineligible)} ineligible, all listed with reason"))

    # ── 8. Buckets populated + score distribution ────────────────────────────
    buckets = {b: sum(1 for r in snap.rows if r.bucket == b)
               for b in ("TOP_10", "TOP_20", "TOP_50", "BOTTOM_50")}
    scores = [r.score for r in ranked]
    dist_ok = bool(scores) and max(scores) >= 99 and min(scores) <= 1 \
        and all(v > 0 for v in buckets.values())
    results.append(_check("RS buckets populated + distribution reasonable", dist_ok,
                          "  ".join(f"{k}={v}" for k, v in buckets.items())))

    # Leadership summary
    from collections import Counter
    cnt = Counter(r.status for r in snap.rows)
    print(f"\n  {DIM}Leadership mix: " +
          "  ".join(f"{k}={cnt.get(k,0)}" for k in valid_status) + RST)

    print(f"\n  {'='*64}")
    passed = sum(results)
    ok = passed == len(results)
    print(f"  {(GREEN if ok else RED)}{passed}/{len(results)} checks passed{RST}")
    print(f"  {'='*64}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
