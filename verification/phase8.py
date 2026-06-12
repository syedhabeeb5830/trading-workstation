"""
verification/phase8.py — Phase 8 (Edge Validation) acceptance checks
====================================================================
Proves the Phase-8 contract (must pass 8/8) and prints the edge report.

  1. Forward returns generated.
  2. Every screen date processed (walk-forward replay).
  3. Grade buckets analyzed.
  4. Actionability buckets analyzed.
  5. Regime analysis completed.
  6. Adaptive vs Composite completed.
  7. Portfolio simulation completed.
  8. EDGE_SCORE generated (0..100).

Usage:
    python verification/phase8.py
    python verification/phase8.py --sample 250
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

from analytics.edge_validation import (                                       # noqa: E402
    run_edge_validation, render_edge_report, PRIMARY_HORIZON, _HISTORY_DIR)

logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")
GREEN, RED, DIM, RST = "\033[92m", "\033[91m", "\033[2m", "\033[0m"


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=200)
    args = ap.parse_args()

    print(f"\n  {'='*64}\n  PHASE 8 VERIFICATION — Edge Validation\n  {'='*64}")
    offsets = (200, 160, 120, 90)
    print(f"  {DIM}Walk-forward replay at {len(offsets)} past dates "
          f"(sample={args.sample or 'full'}, 2y data)...{RST}")
    snap, records = run_edge_validation(period="2y", sample=args.sample,
                                        offsets=offsets, persist=True, export=True)
    render_edge_report(snap)

    print(f"  {'─'*64}\n  CHECKS\n  {'─'*64}")
    results: list[bool] = []
    H = PRIMARY_HORIZON

    # ── 1. Forward returns generated ─────────────────────────────────────────
    with_fwd = sum(1 for r in records if r.fwd.get(H) is not None)
    results.append(_check("Forward returns generated",
                          with_fwd > 0, f"{with_fwd}/{len(records)} records have {H}D return"))

    # ── 2. Every screen date processed ───────────────────────────────────────
    n_dates = len({r.screen_date for r in records})
    results.append(_check("Every screen date processed (walk-forward)",
                          n_dates == len(offsets),
                          f"{n_dates}/{len(offsets)} as-of dates produced records"))

    # ── 3. Grade buckets ─────────────────────────────────────────────────────
    results.append(_check("Grade buckets analyzed", len(snap.grade_perf) >= 3,
                          " ".join(f"{g}={v['avg_return']}" for g, v in snap.grade_perf.items())))

    # ── 4. Actionability buckets ─────────────────────────────────────────────
    results.append(_check("Actionability buckets analyzed", len(snap.action_perf) >= 1,
                          " ".join(f"{c}={v['avg_return']}" for c, v in snap.action_perf.items())))

    # ── 5. Regime analysis ───────────────────────────────────────────────────
    results.append(_check("Regime analysis completed", len(snap.regime_perf) >= 1,
                          " ".join(snap.regime_perf.keys())))

    # ── 6. Adaptive vs Composite ─────────────────────────────────────────────
    avc = snap.adaptive_vs_composite
    results.append(_check("Adaptive vs Composite completed",
                          {"composite_alpha", "adaptive_alpha", "improvement"} <= set(avc),
                          f"comp={avc['composite_alpha']}% adp={avc['adaptive_alpha']}% "
                          f"impr={avc['improvement']}%"))

    # ── 7. Portfolio simulation ──────────────────────────────────────────────
    p = snap.portfolio
    results.append(_check("Portfolio simulation completed",
                          "n_trades" in p and "basis" in p,
                          f"trades={p['n_trades']} avg={p.get('avg_return')} "
                          f"sharpe={p.get('sharpe')}"))

    # ── 8. EDGE_SCORE ────────────────────────────────────────────────────────
    results.append(_check("EDGE_SCORE generated (0..100)",
                          0 <= snap.edge_score <= 100,
                          f"{snap.edge_score} → {snap.verdict}"))

    # Persistence example
    jp = _HISTORY_DIR / f"edge_{snap.generated_at[:10]}.json"
    print(f"\n  {DIM}persisted: {jp.exists()} ({jp}); "
          f"reports/edge_report_{snap.generated_at[:10]}.md{RST}")

    print(f"\n  {'='*64}")
    passed = sum(results)
    ok = passed == len(results)
    print(f"  {(GREEN if ok else RED)}{passed}/{len(results)} checks passed{RST}")
    print(f"  {'='*64}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
