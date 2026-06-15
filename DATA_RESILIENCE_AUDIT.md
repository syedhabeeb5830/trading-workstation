# Data Resilience Hardening — Audit & Fix Record (Phase)

> **Date:** 2026-06-12 · **Status: FIXED + VERIFIED (13/13).**
> Reliability-only phase. **No scoring / regime / composite / actionability / portfolio
> math changed.** Read alongside `HANDOFF.md`. Companion forensic trace preceded this fix.

---

## The bug (what we fixed)

A failed `^NSEI` download silently converted *missing data* into a *bearish market*:

```
^NSEI download fails  →  fetch_nifty() = None
   →  RS eligibility gate hard-required the benchmark (RS:171-173)
   →  EVERY stock ineligible → forced status=NEUTRAL, score=0
   →  leadership 95→0 · composite −15 (leaders) · regime NEUTRAL→WEAK_BEAR · ACTION_NOW 4→0
   →  and the trader saw "data=100%" with NO indication anything failed
```

Root cause: [relative_strength_engine.py:171-173](scanner/relative_strength_engine.py#L171-L173) — a sector-relative RS fallback already existed but was discarded by the eligibility gate.

## The fix (four layers, defense-in-depth)

**1. RS eligibility — honour the sector fallback.** Eligibility no longer hard-requires the
Nifty benchmark; a stock with its own history + a composite RS (benchmark-blended *or*
sector-relative) is eligible. The snapshot now carries `rs_mode` (`FULL` | `SECTOR_FALLBACK`)
and `benchmark_status` (`OK` | `MISSING`). **Invariant:** when the benchmark is present,
`nifty_returns[r3m]` is non-None for every stock, so the dropped clause was constant-True —
behaviour is byte-identical. The change is confined entirely to the benchmark-missing path.
([relative_strength_engine.py](scanner/relative_strength_engine.py))

**2. Benchmark cache — mirror the stock-OHLCV pattern.** `fetch_benchmark()` does
live → write-through `cache/benchmarks/<ticker>_<period>.csv` → on live failure serve the
last good cache → only if both fail report `missing`. `fetch_nifty()` is kept as a thin
backward-compatible wrapper, so all existing callers are unchanged.

**3. Loud surfacing — DATA QUALITY block + degraded banner.** The cockpit now prints
per-source coverage and an overall `HEALTHY / DEGRADED / FAILED` verdict, plus a red
"BENCHMARK DATA UNAVAILABLE — DEGRADED MODE / Regime confidence: LOW / Missing data is NOT
bearish" banner whenever RS is in fallback. ([screen_runner.py](screen/screen_runner.py))

**4. Fail-loud abort (policy decision: Option A).** When `^NSEI` is gone from **both** live
and cache, `build_screen` raises `BenchmarkUnavailableError`; `run_screen`, `run_portfolio`,
and `run_review_portfolio` print a "SCREEN ABORTED" banner and return exit code 2 — **no
regime, no board, never a false-bearish read.** A `build_screen(allow_degraded=True)` escape
hatch forces sector-relative fallback for research only.

## Resulting policy

| `^NSEI` state | Verdict | Behaviour |
|---|---|---|
| live download OK | **HEALTHY** | normal — identical to before |
| live failed, **cache hit** | **DEGRADED** | continue on last-known benchmark (RS still FULL); banner shown |
| live **and** cache both fail | **FAILED** | **ABORT** (fail loud); `allow_degraded=True` → SECTOR_FALLBACK |

Optional benchmarks `^CRSLDX` / `^INDIAVIX` were already resilient (trend → 50 NEUTRAL,
volatility → 60 NORMAL on loss) and are unchanged; their loss only downgrades to DEGRADED.

## Verification (13/13 — `python verification/data_resilience.py`)

- **Invariant:** live `--screen` → DATA QUALITY HEALTHY, regime NEUTRAL 52.6, board unchanged
  (ACTION_NOW=4, WATCHLIST=69, EXTENDED=8, AVOID=405 — identical to pre-fix).
- **Bug fixed:** with benchmark missing, eligible stocks **500** (was 0), **100** sector
  leaders survive (was 0), `rs_mode=SECTOR_FALLBACK`, `benchmark_status=MISSING`.
- **Cascade (forced total outage):** composite −2.5 (was −15), ACTION_NOW 4→2 (was →0),
  leadership 95→45 (was →0) — collapse eliminated; in production this path now **aborts**.
- **Fail-loud:** `build_screen` raises when `^NSEI` gone; `allow_degraded=True` bypasses.
- **Cache:** benchmark write→read roundtrip preserves data.
- **Banner logic:** all-live→HEALTHY, ^NSEI-missing→FAILED, ^NSEI-cached→DEGRADED.

## Files changed (reliability-only)

- `scanner/relative_strength_engine.py` — benchmark cache + `fetch_benchmark`/`BenchmarkResult`; eligibility fix; `rs_mode`/`benchmark_status`.
- `screen/screen_runner.py` — data-quality verdict, DATA QUALITY render + degraded banner, `BenchmarkUnavailableError`, fail-loud abort + `allow_degraded`.
- `portfolio/portfolio_engine.py`, `portfolio/lifecycle_engine.py` — catch abort at entry points.
- `verification/data_resilience.py` *(new)* — 13-check acceptance suite (offline/deterministic).

## What is explicitly NOT changed
Composite weights, RS classification thresholds, regime weights/labels math, actionability
thresholds, portfolio sizing/caps. Trust is restored without touching a single scoring rule.

## Next (per the agreed roadmap)
Data resilience (this) → ACTION_NOW 5-year validation → portfolio A/B harness → lifecycle
tracking → RR predictor validation.
