"""
verification/data_resilience.py — Data Resilience Hardening acceptance
======================================================================
Proves the core reliability guarantee:

    Missing benchmark data is NEVER treated as bearish data.

and that the benchmark-PRESENT path is unchanged. Reliability-only: this exercises
the RS engine, the benchmark cache, and the data-quality verdict — it asserts NO
scoring values and changes no logic.

Runs OFFLINE off the same-day OHLCV cache (no network), using a synthetic
benchmark for the "present" baseline so the result is deterministic.

    python verification/data_resilience.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import pandas as pd

from scanner.universe_builder import build_universe
from scanner.data_feed import get_ohlcv
from scanner.sector_engine import SectorRanker
from scanner.relative_strength_engine import (
    RelativeStrengthRanker, _save_bench_cache, _load_bench_cache)
from screen.screen_runner import _assess_data_quality

G, R, Y, RST = "\033[92m", "\033[91m", "\033[93m", "\033[0m"
_results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    _results.append(bool(ok))
    mark = f"{G}PASS{RST}" if ok else f"{R}FAIL{RST}"
    print(f"  [{mark}] {label}" + (f"   ({detail})" if detail else ""))


def main() -> int:
    print(f"\n  {'='*68}\n  DATA RESILIENCE HARDENING — ACCEPTANCE\n  {'='*68}\n")

    # ── Offline inputs from the same-day cache ────────────────────────────────
    uni = build_universe()
    feed = get_ohlcv(uni.tickers, period="1y", min_bars=30)
    if not feed.data:
        print(f"  {R}No cached OHLCV available — run `python run.py --screen` once first.{RST}")
        return 1
    sector_snap = SectorRanker(uni.sector_map, feed.data, uni.source).rank(persist=False)

    # Synthetic "present" benchmark = equal-weight close of a liquid subset.
    closes = [df["Close"] for df in list(feed.data.values())[:60]
              if df is not None and len(df) > 200]
    bench = pd.concat(closes, axis=1).dropna().mean(axis=1).to_frame("Close")
    for col in ("Open", "High", "Low"):
        bench[col] = bench["Close"]
    bench["Volume"] = 0.0

    # ── 1. FULL mode (benchmark PRESENT) — normal behaviour intact ────────────
    full = RelativeStrengthRanker(feed.data, uni.sector_map, bench, sector_snap).rank()
    ml_full = sum(1 for r in full.rows if r.status == "MARKET_LEADER")
    elig_full = sum(1 for r in full.rows if r.rank is not None)
    check("FULL: flags rs_mode=FULL / benchmark_status=OK when benchmark present",
          full.rs_mode == "FULL" and full.benchmark_status == "OK", full.rs_mode)
    check("FULL: MARKET_LEADERs are still detected (normal mode intact)",
          ml_full > 0, f"{ml_full} market leaders, {elig_full} eligible")

    # ── 2. MISSING benchmark (the bug scenario): nifty_df = None ──────────────
    deg = RelativeStrengthRanker(feed.data, uni.sector_map, None, sector_snap).rank()
    elig_deg = sum(1 for r in deg.rows if r.rank is not None)
    neutral_deg = sum(1 for r in deg.rows if r.status == "NEUTRAL")
    sector_leaders = sum(1 for r in deg.rows
                         if r.status in ("SECTOR_LEADER", "EMERGING_LEADER"))
    check("MISSING: degrades gracefully (no crash)", deg is not None)
    check("MISSING: flags rs_mode=SECTOR_FALLBACK / benchmark_status=MISSING",
          deg.rs_mode == "SECTOR_FALLBACK" and deg.benchmark_status == "MISSING")
    check("MISSING: does NOT force every stock to NEUTRAL (bug fixed)",
          neutral_deg < len(deg.rows), f"{neutral_deg}/{len(deg.rows)} NEUTRAL")
    check("MISSING: eligible stocks remain (was 0 before the fix)",
          elig_deg > 0, f"{elig_deg} eligible")
    check("MISSING: leadership survives via sector-relative fallback (NOT zero)",
          sector_leaders > 0, f"{sector_leaders} sector/emerging leaders")

    # ── 3. Benchmark cache write→read roundtrip ───────────────────────────────
    _save_bench_cache("^TESTIDX", "1y", bench)
    rt = _load_bench_cache("^TESTIDX", "1y")
    same = (rt is not None and len(rt) == len(bench)
            and abs(float(rt["Close"].iloc[-1]) - float(bench["Close"].iloc[-1])) < 1e-6)
    check("CACHE: benchmark write→read roundtrip preserves data", same)
    try:
        (Path("cache/benchmarks") / "TESTIDX_1y.csv").unlink()
    except Exception:
        pass

    # ── 4. Data-quality verdict logic (the cockpit banner) ────────────────────
    class _F:  # minimal feed stub
        coverage = 1.0

    class _B:
        def __init__(self, st): self.status = st

    class _RS:
        def __init__(self, m, b): self.rs_mode, self.benchmark_status = m, b

    healthy = _assess_data_quality(_F(), _B("live"), _B("live"), _B("live"),
                                   _RS("FULL", "OK"))
    failed = _assess_data_quality(_F(), _B("missing"), _B("missing"), _B("missing"),
                                  _RS("SECTOR_FALLBACK", "MISSING"))
    cached = _assess_data_quality(_F(), _B("cache"), _B("live"), _B("live"),
                                  _RS("FULL", "OK"))
    check("BANNER: all-live → HEALTHY", healthy["status"] == "HEALTHY")
    check("BANNER: ^NSEI missing → FAILED + SECTOR_FALLBACK surfaced",
          failed["status"] == "FAILED" and failed["rs_mode"] == "SECTOR_FALLBACK")
    check("BANNER: ^NSEI cached (live failed) → DEGRADED", cached["status"] == "DEGRADED")

    # ── 5. Fail-loud: build_screen aborts when ^NSEI is gone (live + cache fail) ──
    import screen.screen_runner as _sr
    from screen.screen_runner import BenchmarkUnavailableError
    from scanner.relative_strength_engine import BenchmarkResult as _BR
    _orig = _sr.fetch_benchmark
    _sr.fetch_benchmark = lambda period="1y", ticker="^NSEI": _BR(ticker, None, "missing")
    raised = False
    try:
        _sr.build_screen(persist=False)
    except BenchmarkUnavailableError:
        raised = True
    except Exception:
        raised = False
    finally:
        _sr.fetch_benchmark = _orig
    check("ABORT: build_screen fails loud when ^NSEI gone (no false-bearish board)", raised)

    # allow_degraded escape hatch still permits sector-fallback (research only)
    _sr.fetch_benchmark = lambda period="1y", ticker="^NSEI": _BR(ticker, None, "missing")
    try:
        degraded = _sr.build_screen(persist=False, allow_degraded=True)
        ok_deg = (degraded.data_quality.get("rs_mode") == "SECTOR_FALLBACK"
                  and degraded.act_snap.classification_distribution()["ACTION_NOW"] >= 0)
    except Exception:
        ok_deg = False
    finally:
        _sr.fetch_benchmark = _orig
    check("ESCAPE: allow_degraded=True bypasses abort → SECTOR_FALLBACK board", ok_deg)

    # ── Result ────────────────────────────────────────────────────────────────
    ok = all(_results)
    print(f"\n  {(G if ok else R)}{sum(_results)}/{len(_results)} checks passed{RST}")
    print(f"  {'='*68}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
