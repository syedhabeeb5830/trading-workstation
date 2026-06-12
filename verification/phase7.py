"""
verification/phase7.py — Phase 7 (Regime-Aware Scoring) acceptance checks
=========================================================================
Proves the Phase-7 contract (must pass 8/8) + prints the adaptive board,
promotion/demotion report, and a persistence example.

  1. Adaptive scores generated (one per composite survivor).
  2. Scores bounded 0..100.
  3. Weights sum to 100 for every regime profile.
  4. Every regime maps to a profile.
  5. Deterministic across repeated runs.
  6. Different regimes produce different rankings (STRONG_BULL vs STRONG_BEAR).
  7. Promotion/demotion report generated.
  8. History persisted (Journal/adaptive_history JSON + CSV).
"""

from __future__ import annotations

import dataclasses
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from screen.screen_runner import build_screen, _render_adaptive               # noqa: E402
from scanner.adaptive_scoring import (                                        # noqa: E402
    AdaptiveScorer, WeightProfileFactory, _HISTORY_DIR, _HISTORY_CSV)

logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")
GREEN, RED, DIM, BOLD, RST = "\033[92m", "\033[91m", "\033[2m", "\033[1m", "\033[0m"
REGIMES = ["STRONG_BULL", "BULL", "NEUTRAL", "RANGE", "WEAK_BEAR", "STRONG_BEAR", "VOLATILE"]


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))
    return ok


def main() -> int:
    print(f"\n  {'='*64}\n  PHASE 7 VERIFICATION — Regime-Aware Scoring\n  {'='*64}")
    results: list[bool] = []

    res = build_screen(period="1y", persist=True)
    adp = res.adaptive_snap
    factory = WeightProfileFactory()
    scorer = AdaptiveScorer(factory)

    # Sample output
    _render_adaptive(adp)
    print(f"\n  {DIM}weight profile source: {factory.source}{RST}")

    print(f"\n  {'─'*64}\n  CHECKS\n  {'─'*64}")

    # ── 1. Adaptive scores generated ─────────────────────────────────────────
    results.append(_check("Adaptive scores generated (one per survivor)",
                          len(adp.rows) == len(res.comp_snap.rows) and len(adp.rows) > 0,
                          f"{len(adp.rows)} rows"))

    # ── 2. Bounded 0..100 ────────────────────────────────────────────────────
    results.append(_check("Scores bounded 0..100",
                          all(0 <= r.adaptive_score <= 100 for r in adp.rows),
                          f"min={min(r.adaptive_score for r in adp.rows):.1f} "
                          f"max={max(r.adaptive_score for r in adp.rows):.1f}"))

    # ── 3. Weights sum to 100 per profile ────────────────────────────────────
    sums = {r: factory.for_regime(r).total() for r in REGIMES}
    results.append(_check("Weights sum to 100 for every profile",
                          all(abs(s - 100) < 0.01 for s in sums.values()),
                          "  ".join(f"{r}={s:g}" for r, s in sums.items())))

    # ── 4. Every regime maps to a profile ────────────────────────────────────
    mapped = all(set(factory.for_regime(r).weights) and
                 factory.for_regime(r).total() > 0 for r in REGIMES)
    results.append(_check("Every regime maps to a profile", mapped,
                          f"{len(REGIMES)} regimes resolved"))

    # ── 5. Deterministic ─────────────────────────────────────────────────────
    a1 = scorer.score(res.comp_snap, res.regime)
    a2 = scorer.score(res.comp_snap, res.regime)
    sig1 = [(r.ticker, r.adaptive_rank, r.adaptive_score) for r in a1.rows]
    sig2 = [(r.ticker, r.adaptive_rank, r.adaptive_score) for r in a2.rows]
    results.append(_check("Deterministic across repeated runs", sig1 == sig2))

    # ── 6. Different regimes ⇒ different rankings ────────────────────────────
    bull_regime = dataclasses.replace(res.regime, regime="STRONG_BULL")
    bear_regime = dataclasses.replace(res.regime, regime="STRONG_BEAR")
    bull = scorer.score(res.comp_snap, bull_regime)
    bear = scorer.score(res.comp_snap, bear_regime)
    bull_order = [r.ticker for r in bull.top_n(20)]
    bear_order = [r.ticker for r in bear.top_n(20)]
    results.append(_check("Different regimes produce different rankings",
                          bull_order != bear_order,
                          f"top-20 differ; bull#1={bull_order[0]} bear#1={bear_order[0]}"))

    # ── 7. Promotion/demotion report ─────────────────────────────────────────
    proms, dems = bear.top_promotions(5), bear.top_demotions(5)
    results.append(_check("Promotion/demotion report generated",
                          len(proms) > 0 and len(dems) > 0,
                          f"{len(proms)} promotions, {len(dems)} demotions (STRONG_BEAR)"))

    # ── 8. History persisted ─────────────────────────────────────────────────
    jp = _HISTORY_DIR / f"{adp.week_id}.json"
    results.append(_check("History persisted (JSON + CSV)",
                          jp.exists() and _HISTORY_CSV.exists(), f"{jp}"))

    # Persistence example
    print(f"\n  {BOLD}══ PERSISTENCE EXAMPLE (adaptive_history.csv head) ══{RST}")
    try:
        import pandas as pd
        df = pd.read_csv(_HISTORY_CSV)
        print(df.head(6).to_string(index=False))
    except Exception as exc:
        print(f"  (could not read CSV: {exc})")

    print(f"\n  {'='*64}")
    passed = sum(results)
    ok = passed == len(results)
    print(f"  {(GREEN if ok else RED)}{passed}/{len(results)} checks passed{RST}")
    print(f"  {'='*64}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
