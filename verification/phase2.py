"""
verification/phase2.py — Phase 2 (Sector Leadership Engine) acceptance checks
=============================================================================
Proves the Phase-2 contract:

  1. Every universe stock is mapped to a sector.
  2. Sector count matches the universe (no sector silently skipped).
  3. All sectors receive rankings (contiguous, unique).
  4. Rankings are deterministic across repeated runs.
  5. Missing data is handled gracefully (no crash; insufficient sectors listed).
  6. Score distribution is reasonable.
  7. Weekly snapshot is persisted (JSON + CSV).

Usage:
    python verification/phase2.py
    python verification/phase2.py --sample 120   # quick smoke
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from scanner.universe_builder import build_universe          # noqa: E402
from scanner.data_feed import get_ohlcv                       # noqa: E402
from scanner.sector_engine import SectorRanker, _HISTORY_CSV  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")
GREEN, RED, DIM, BOLD, RST = "\033[92m", "\033[91m", "\033[2m", "\033[1m", "\033[0m"


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))
    return ok


def _grade_color(grade: str) -> str:
    return {"A+": GREEN, "A": GREEN}.get(grade, "")


def _print_board(snap) -> None:
    print(f"\n  {BOLD}{'#':>3}  {'SECTOR':28s}  {'SCORE':>5}  {'GR':>3}  "
          f"{'STATUS':10s}  {'1M':>7}  {'3M':>7}  {'6M':>7}  {'N':>4}{RST}")
    print(f"  {'─'*86}")
    for r in snap.rows:
        d = f" ({r.rank_delta:+d})" if r.rank_delta else ""
        gc = _grade_color(r.grade)
        r1 = f"{r.r1m:+.1f}" if r.r1m is not None else "—"
        r3 = f"{r.r3m:+.1f}" if r.r3m is not None else "—"
        r6 = f"{r.r6m:+.1f}" if r.r6m is not None else "—"
        print(f"  {r.rank:>3}  {r.sector[:28]:28s}  {r.score:>5.1f}  "
              f"{gc}{r.grade:>3}{RST}  {r.status:10s}  {r1:>7}  {r3:>7}  {r6:>7}  "
              f"{r.n_valid:>2}/{r.n_constituents:<2}{d}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--period", default="1y")
    args = ap.parse_args()

    print(f"\n  {'='*64}\n  PHASE 2 VERIFICATION — Sector Leadership Engine\n  {'='*64}\n")
    results: list[bool] = []

    uni = build_universe()
    tickers = uni.tickers[: args.sample] if args.sample else uni.tickers
    sub = uni.df[uni.df["ticker"].isin(tickers)]
    sector_map = dict(zip(sub["ticker"], sub["sector"]))

    # ── 1. Every stock mapped to a sector ────────────────────────────────────
    unmapped = [t for t in tickers if not sector_map.get(t)]
    results.append(_check("Every universe stock mapped to a sector",
                          len(unmapped) == 0,
                          f"{len(tickers)} stocks, {len(unmapped)} unmapped"))

    print(f"\n  {DIM}Fetching {len(tickers)} tickers @ {args.period}...{RST}")
    feed = get_ohlcv(tickers, period=args.period, min_bars=30)
    print(f"  {DIM}coverage {feed.coverage*100:.1f}% ({feed.succeeded}/{feed.requested}), "
          f"{feed.elapsed_s:.1f}s, source={feed.source}{RST}")

    ranker = SectorRanker(sector_map, feed.data, universe_source=uni.source)
    snap = ranker.rank(persist=True)
    _print_board(snap)

    universe_sectors = set(sector_map.values())
    snap_sectors = {r.sector for r in snap.rows}

    # ── 2. Sector count matches; none skipped ────────────────────────────────
    results.append(_check("Sector count matches universe (none skipped)",
                          snap_sectors == universe_sectors,
                          f"universe={len(universe_sectors)}  ranked={len(snap_sectors)}"))

    # ── 3. All sectors ranked (contiguous, unique) ───────────────────────────
    ranks = sorted(r.rank for r in snap.rows)
    results.append(_check("All sectors ranked (contiguous & unique)",
                          ranks == list(range(1, len(snap.rows) + 1)),
                          f"{len(ranks)} ranks, 1..{len(snap.rows)}"))

    # ── 4. Deterministic across repeated runs ────────────────────────────────
    snap2 = SectorRanker(sector_map, feed.data, uni.source).rank(persist=False)
    sig1 = [(r.sector, r.rank, r.score) for r in snap.rows]
    sig2 = [(r.sector, r.rank, r.score) for r in snap2.rows]
    results.append(_check("Rankings deterministic across runs", sig1 == sig2,
                          "identical order + scores"))

    # ── 5. Missing data handled gracefully ───────────────────────────────────
    insufficient = [r for r in snap.rows if r.composite is None]
    graceful = all(r.status and r.reasons for r in insufficient)
    results.append(_check("Missing data handled gracefully",
                          graceful,
                          f"{len(insufficient)} sector(s) below data floor, all listed"))

    # ── 6. Score distribution reasonable ─────────────────────────────────────
    scored = [r.score for r in snap.rows if r.composite is not None]
    spread_ok = bool(scored) and max(scored) >= 95 and min(scored) <= 5 \
        and len(set(round(s) for s in scored)) >= max(3, len(scored) // 3)
    results.append(_check("Score distribution reasonable", spread_ok,
                          f"min={min(scored):.0f} max={max(scored):.0f} "
                          f"n_scored={len(scored)}" if scored else "no scored sectors"))

    # ── 7. Snapshot persisted ────────────────────────────────────────────────
    json_path = Path("Journal/sector_history") / f"{snap.week_id}.json"
    results.append(_check("Weekly snapshot persisted (JSON + CSV)",
                          json_path.exists() and _HISTORY_CSV.exists(),
                          f"{json_path}  +  {_HISTORY_CSV}"))

    print(f"\n  {'='*64}")
    passed = sum(results)
    ok = passed == len(results)
    print(f"  {(GREEN if ok else RED)}{passed}/{len(results)} checks passed{RST}")
    print(f"  {'='*64}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
