"""
verification/phase1.py — Phase 1 (Data Foundation) acceptance checks
====================================================================
Verifies the contract agreed for Phase 1:

  1. Nifty 500 universe loads successfully (live, cache, or emergency).
  2. At least 95% of symbols receive valid OHLCV data.
  3. A second run is served from cache.
  4. The cached run is significantly faster than the download run.

Usage:
    python verification/phase1.py            # full universe
    python verification/phase1.py --sample 40   # quick smoke on a subset
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows consoles default to cp1252 — force UTF-8 so box/■ glyphs don't crash.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from scanner.universe_builder import build_universe  # noqa: E402
from scanner.data_feed import get_ohlcv              # noqa: E402

logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")

GREEN, RED, DIM, RST = "\033[92m", "\033[91m", "\033[2m", "\033[0m"


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0,
                    help="limit to N tickers for a quick smoke test")
    ap.add_argument("--period", default="6mo")
    args = ap.parse_args()

    print(f"\n  {'='*64}\n  PHASE 1 VERIFICATION — Data Foundation\n  {'='*64}\n")
    results: list[bool] = []

    # ── 1. Universe loads ────────────────────────────────────────────────────
    uni = build_universe(force_refresh=True)
    n_sectors = uni.df["sector"].nunique()
    results.append(_check(
        "Nifty 500 universe loads",
        uni.symbol_count >= (50 if args.sample else 400),
        f"source={uni.source}  symbols={uni.symbol_count}  sectors={n_sectors}",
    ))
    if uni.warnings:
        for w in uni.warnings:
            print(f"      {DIM}note: {w}{RST}")

    tickers = uni.tickers
    if args.sample:
        tickers = tickers[: args.sample]
    print(f"\n  {DIM}Fetching OHLCV for {len(tickers)} tickers "
          f"(period={args.period})...{RST}")

    # ── 2. Coverage ≥ 95% on a fresh download ────────────────────────────────
    dl = get_ohlcv(tickers, period=args.period, force_refresh=True)
    results.append(_check(
        "≥95% symbols receive valid data",
        dl.coverage >= 0.95,
        f"coverage={dl.coverage*100:.1f}%  "
        f"({dl.succeeded}/{dl.requested})  {dl.elapsed_s:.1f}s",
    ))
    if dl.failed:
        shown = ", ".join(dl.failed[:12]) + ("..." if len(dl.failed) > 12 else "")
        print(f"      {DIM}failed: {shown}{RST}")

    # ── 3. Second run is served from cache ───────────────────────────────────
    cached = get_ohlcv(tickers, period=args.period)
    results.append(_check(
        "Re-run is served from cache",
        cached.source == "cache" and cached.succeeded > 0,
        f"source={cached.source}  symbols={cached.succeeded}  {cached.elapsed_s:.1f}s",
    ))

    # ── 4. Cache run is meaningfully faster ──────────────────────────────────
    speedup = (dl.elapsed_s / cached.elapsed_s) if cached.elapsed_s > 0 else 999
    results.append(_check(
        "Cached run significantly faster",
        cached.elapsed_s < dl.elapsed_s and (speedup >= 3 or cached.elapsed_s < 2.0),
        f"download={dl.elapsed_s:.1f}s  cache={cached.elapsed_s:.1f}s  "
        f"speedup={speedup:.1f}x",
    ))

    print(f"\n  {'='*64}")
    passed = sum(results)
    ok = passed == len(results)
    color = GREEN if ok else RED
    print(f"  {color}{passed}/{len(results)} checks passed{RST}")
    print(f"  {'='*64}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
