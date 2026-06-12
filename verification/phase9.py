"""
verification/phase9.py — Phase 9 (Portfolio Construction) acceptance checks
===========================================================================
Proves the Phase-9 contract (must pass 8/8) + prints the sample portfolio,
allocation table, sector exposure, correlation report, and JSON export.

  1. Portfolio generated.
  2. Allocations sum within the exposure limit (and per-name ≤ cap).
  3. Sector limits respected.
  4. Correlation-cluster limits respected.
  5. Regime exposure respected.
  6. Deterministic.
  7. Portfolio metrics generated.
  8. Exports (JSON/CSV/MD) generated.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from screen.screen_runner import build_screen                                  # noqa: E402
from portfolio.portfolio_engine import (                                       # noqa: E402
    PortfolioConstructor, render_portfolio, export_portfolio,
    REGIME_EXPOSURE, MAX_SECTOR_EXPOSURE, MAX_CLUSTER_EXPOSURE, MAX_POSITION_SIZE)

logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")
GREEN, RED, DIM, BOLD, RST = "\033[92m", "\033[91m", "\033[2m", "\033[1m", "\033[0m"
EPS = 0.5


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))
    return ok


def _sector_exposure(snap) -> dict:
    out = {}
    for p in snap.positions:
        out[p.sector] = out.get(p.sector, 0.0) + p.allocation
    return out


def _cluster_exposure(snap) -> dict:
    out = {}
    for p in snap.positions:
        out[p.cluster_id] = out.get(p.cluster_id, 0.0) + p.allocation
    return out


def main() -> int:
    print(f"\n  {'='*64}\n  PHASE 9 VERIFICATION — Portfolio Construction\n  {'='*64}")
    res = build_screen(period="1y", persist=False)
    constructor = PortfolioConstructor()
    snap = constructor.construct(res.act_snap, res.regime, res.feed.data, persist=True)

    render_portfolio(snap)
    print(f"  {BOLD}══ EXAMPLE JSON EXPORT ══{RST}")
    print(snap.export_json()[:900])

    print(f"\n  {'─'*64}\n  CHECKS\n  {'─'*64}")
    results: list[bool] = []

    # ── 1. Portfolio generated ───────────────────────────────────────────────
    results.append(_check("Portfolio generated",
                          snap is not None and hasattr(snap, "positions")
                          and isinstance(snap.metrics, dict),
                          f"{len(snap.positions)} positions, regime {snap.regime}"))

    # ── 2. Allocations within exposure + per-name cap ────────────────────────
    per_name_ok = all(p.allocation <= MAX_POSITION_SIZE + EPS for p in snap.positions)
    results.append(_check("Allocations within exposure limit",
                          snap.deployed <= snap.recommended_exposure + EPS and per_name_ok
                          and abs(snap.deployed + snap.cash - 100) < 0.01,
                          f"deployed {snap.deployed}% ≤ {snap.recommended_exposure}%, "
                          f"cash {snap.cash}%"))

    # ── 3. Sector limits ─────────────────────────────────────────────────────
    sec = _sector_exposure(snap)
    worst_sec = max(sec.values(), default=0.0)
    results.append(_check(f"Sector limits respected (≤ {MAX_SECTOR_EXPOSURE}%)",
                          worst_sec <= MAX_SECTOR_EXPOSURE + EPS,
                          f"max sector exposure {worst_sec:.1f}%"))

    # ── 4. Correlation-cluster limits ────────────────────────────────────────
    clu = _cluster_exposure(snap)
    worst_clu = max(clu.values(), default=0.0)
    results.append(_check(f"Correlation-cluster limits respected (≤ {MAX_CLUSTER_EXPOSURE}%)",
                          worst_clu <= MAX_CLUSTER_EXPOSURE + EPS,
                          f"max cluster exposure {worst_clu:.1f}%"))

    # ── 5. Regime exposure respected ─────────────────────────────────────────
    expected_exp = REGIME_EXPOSURE.get(snap.regime, 50)
    results.append(_check("Regime exposure respected",
                          snap.recommended_exposure == expected_exp
                          and snap.deployed <= expected_exp + EPS,
                          f"{snap.regime} → {expected_exp}% cap"))

    # ── 6. Deterministic ─────────────────────────────────────────────────────
    snap2 = PortfolioConstructor().construct(res.act_snap, res.regime, res.feed.data)
    sig1 = [(p.ticker, p.allocation, p.conviction) for p in snap.positions]
    sig2 = [(p.ticker, p.allocation, p.conviction) for p in snap2.positions]
    results.append(_check("Deterministic across repeated runs", sig1 == sig2))

    # ── 7. Metrics generated ─────────────────────────────────────────────────
    need = {"expected_rr", "avg_conviction", "sector_concentration",
            "cluster_concentration", "n_positions"}
    results.append(_check("Portfolio metrics generated", need <= set(snap.metrics),
                          f"{', '.join(sorted(need))}"))

    # ── 8. Exports ───────────────────────────────────────────────────────────
    paths = export_portfolio(snap)
    results.append(_check("Exports (JSON/CSV/MD) generated",
                          all(p.exists() and p.stat().st_size > 0 for p in paths.values()),
                          "  ".join(str(p) for p in paths.values())))

    print(f"\n  {'='*64}")
    passed = sum(results)
    ok = passed == len(results)
    print(f"  {(GREEN if ok else RED)}{passed}/{len(results)} checks passed{RST}")
    print(f"  {'='*64}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
