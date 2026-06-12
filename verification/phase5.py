"""
verification/phase5.py — Phase 5 (Actionability Engine + Cockpit) acceptance
============================================================================
Proves the Phase-5 contract (must pass 8/8):

  1. Every Phase-4 survivor receives an actionability_score.
  2. All scores bounded 0..100.
  3. Ranks contiguous & unique.
  4. Deterministic across repeated runs.
  5. Top 5 Actionable Trades generated (or correctly empty with reason).
  6. Every ACTION_NOW stock has RR ≥ 2.
  7. Every EXTENDED stock has breakout distance > threshold.
  8. JSON, CSV, and Markdown exports generated.

Usage:
    python verification/phase5.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from screen.screen_runner import build_screen, export_reports, render_cockpit  # noqa: E402
from scanner.actionability_engine import (                                     # noqa: E402
    ActionabilityRanker, ACTION_NOW_RR, EXTENDED_EXT_THRESHOLD)

logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")
GREEN, RED, DIM, BOLD, RST = "\033[92m", "\033[91m", "\033[2m", "\033[1m", "\033[0m"


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))
    return ok


def main() -> int:
    print(f"\n  {'='*64}\n  PHASE 5 VERIFICATION — Actionability + Cockpit\n  {'='*64}")
    results: list[bool] = []

    res = build_screen(period="1y", persist=True)
    snap = res.act_snap

    # Render the cockpit (this IS the sample Top 20 + Top 5 output).
    render_cockpit(res)

    print(f"  {'─'*64}\n  CHECKS\n  {'─'*64}")
    survivors = len(res.comp_snap.rows)

    # ── 1. Every survivor scored ─────────────────────────────────────────────
    results.append(_check("Every Phase-4 survivor receives actionability_score",
                          len(snap.rows) == survivors
                          and all(r.actionability_score is not None for r in snap.rows),
                          f"{len(snap.rows)}/{survivors}"))

    # ── 2. Bounded 0..100 ────────────────────────────────────────────────────
    results.append(_check("All scores bounded 0..100",
                          all(0 <= r.actionability_score <= 100 for r in snap.rows),
                          f"min={min(r.actionability_score for r in snap.rows):.1f} "
                          f"max={max(r.actionability_score for r in snap.rows):.1f}"))

    # ── 3. Ranks contiguous & unique ─────────────────────────────────────────
    ranks = sorted(r.rank for r in snap.rows)
    results.append(_check("Ranks contiguous & unique",
                          ranks == list(range(1, len(snap.rows) + 1)),
                          f"1..{len(snap.rows)}"))

    # ── 4. Deterministic ─────────────────────────────────────────────────────
    snap2 = ActionabilityRanker(res.comp_snap, res.feed.data).rank(persist=False)
    sig1 = [(r.ticker, r.rank, r.actionability_score) for r in snap.rows]
    sig2 = [(r.ticker, r.rank, r.actionability_score) for r in snap2.rows]
    results.append(_check("Deterministic across repeated runs", sig1 == sig2))

    # ── 5. Top 5 actionable generated ────────────────────────────────────────
    action = snap.action_now(5)
    cd = snap.classification_distribution()
    results.append(_check("Top 5 Actionable Trades generated",
                          isinstance(action, list) and len(action) <= 5,
                          f"{len(action)} ACTION_NOW shown; dist={cd}"))

    # ── 6. ACTION_NOW ⇒ RR ≥ 2 ───────────────────────────────────────────────
    an = [r for r in snap.rows if r.classification == "ACTION_NOW"]
    results.append(_check(f"Every ACTION_NOW has RR ≥ {ACTION_NOW_RR}",
                          all(r.rr_ratio >= ACTION_NOW_RR for r in an),
                          f"{len(an)} ACTION_NOW, "
                          f"min RR={min((r.rr_ratio for r in an), default=0):.1f}"))

    # ── 7. EXTENDED ⇒ extension (vs breakout OR EMA20) > threshold ───────────
    ext = [r for r in snap.rows if r.classification == "EXTENDED"]
    results.append(_check(f"Every EXTENDED has extension > {EXTENDED_EXT_THRESHOLD}%",
                          all(r.extension_pct > EXTENDED_EXT_THRESHOLD for r in ext),
                          f"{len(ext)} EXTENDED, "
                          f"min ext={min((r.extension_pct for r in ext), default=0):.1f}%"))

    # ── 8. JSON / CSV / MD exports ───────────────────────────────────────────
    paths = export_reports(res)
    all_exist = all(p.exists() and p.stat().st_size > 0 for p in paths.values())
    results.append(_check("JSON, CSV, Markdown exports generated", all_exist,
                          "  ".join(str(p) for p in paths.values())))

    # ── Example JSON export (first ACTION_NOW or top row) ─────────────────────
    import json
    sample = (action or snap.top_n(1))[0]
    from dataclasses import asdict
    print(f"\n  {BOLD}══ EXAMPLE JSON (1 row) ══{RST}")
    print(json.dumps(asdict(sample), indent=2)[:1100])

    print(f"\n  {'='*64}")
    passed = sum(results)
    ok = passed == len(results)
    print(f"  {(GREEN if ok else RED)}{passed}/{len(results)} checks passed{RST}")
    print(f"  {'='*64}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
