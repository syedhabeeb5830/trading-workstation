"""
verification/rolling_validation.py — Rolling Validation framework acceptance
============================================================================
Confirms the rolling validation harness (Improvement 2) works and establishes
the baseline EDGE_SCORE that Improvements 1 & 3 will be measured against.

  1. Many weekly screens generated (>> Phase-8's 4).
  2. Large observation sample.
  3. All four horizons (5/10/20/60D) evaluated.
  4. Significance layer computes t-stats.
  5. The four key questions answered every horizon.
  6. EDGE_SCORE bounded 0..100.
  7. Regimes covered.
  8. Persistence + reports written.

Usage:
    python verification/rolling_validation.py            # default sample run
    python verification/rolling_validation.py --sample 200 --years 2 --every 1
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from analytics.rolling_validation import (                                    # noqa: E402
    run_rolling_validation, render_rolling_report)
from analytics.edge_validation import _HISTORY_DIR, _REPORTS_DIR             # noqa: E402

logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")
GREEN, RED, DIM, RST = "\033[92m", "\033[91m", "\033[2m", "\033[0m"


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=120)
    ap.add_argument("--years", type=float, default=2.0)
    ap.add_argument("--every", type=int, default=2)
    args = ap.parse_args()

    print(f"\n  {'='*64}\n  ROLLING VALIDATION — framework acceptance\n  {'='*64}")
    print(f"  {DIM}sample={args.sample or 'full'}  years={args.years}  "
          f"every={args.every}w  (slow: one replay per weekly screen)...{RST}")
    rep, records = run_rolling_validation(period="5y", years=args.years,
                                          every_weeks=args.every, sample=args.sample,
                                          persist=True, export=True)
    render_rolling_report(rep)

    print(f"  {'─'*64}\n  CHECKS\n  {'─'*64}")
    results: list[bool] = []

    results.append(_check("Many weekly screens generated (>> 4)",
                          rep.n_dates >= 20, f"{rep.n_dates} screens"))
    results.append(_check("Large observation sample",
                          rep.n_obs >= 1000, f"{rep.n_obs} observations"))
    results.append(_check("All four horizons evaluated",
                          set(rep.horizons) >= {"5", "10", "20", "60"},
                          ", ".join(rep.horizons)))
    any_t = any(q["t_stat"] is not None
                for h in rep.horizons for q in rep.questions[h].values())
    results.append(_check("Significance layer computes t-stats", any_t))
    keys_ok = all({"grade", "rs", "actionability", "adaptive"} <= set(rep.questions[h])
                  for h in rep.horizons)
    results.append(_check("Four key questions answered every horizon", keys_ok))
    results.append(_check("EDGE_SCORE bounded 0..100",
                          0 <= rep.edge_score <= 100,
                          f"{rep.edge_score} → {rep.verdict}"))
    results.append(_check("Regimes covered", len(rep.regimes) >= 1,
                          " ".join(f"{k}={v}" for k, v in rep.regimes.items())))
    stamp = date.today().isoformat()
    persisted = ((_HISTORY_DIR / f"rolling_{stamp}.json").exists()
                 and (_REPORTS_DIR / f"rolling_report_{stamp}.md").exists())
    results.append(_check("Persistence + reports written", persisted))

    print(f"\n  {'='*64}")
    passed = sum(results)
    ok = passed == len(results)
    print(f"  {(GREEN if ok else RED)}{passed}/{len(results)} checks passed{RST}")
    print(f"  {'='*64}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
