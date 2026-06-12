"""
verification/phase10.py — Phase 10 Priority 1 (Risk-Based Sizing) acceptance
============================================================================
Proves the move from capital allocation → RISK allocation, plus the Risk%
display addition. Must pass 8/8.

  1. Risk % (stop distance) computed for every position.
  2. Risk-based sizing is inverse to stop distance (wider stop → smaller size).
  3. Risk budget honored (uncapped positions risk ≈ conviction-scaled budget).
  4. Portfolio risk metric generated and bounded.
  5. Regime / sector / cluster limits still respected (regression).
  6. Deterministic.
  7. Risk % present in JSON + CSV + MD exports.
  8. Exports generated.
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

from screen.screen_runner import build_screen                                  # noqa: E402
from portfolio.portfolio_engine import (                                       # noqa: E402
    PortfolioConstructor, PositionSizer, render_portfolio, export_portfolio,
    REGIME_EXPOSURE, MAX_SECTOR_EXPOSURE, MAX_CLUSTER_EXPOSURE, MAX_POSITION_SIZE,
    RISK_PER_TRADE_MAX)

logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")
GREEN, RED, DIM, BOLD, RST = "\033[92m", "\033[91m", "\033[2m", "\033[1m", "\033[0m"
EPS = 0.5


def _check(label, ok, detail=""):
    mark = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))
    return ok


def main() -> int:
    print(f"\n  {'='*64}\n  PHASE 10 — RISK-BASED SIZING (Priority 1)\n  {'='*64}")
    res = build_screen(period="1y", persist=False)
    snap = PortfolioConstructor().construct(res.act_snap, res.regime, res.feed.data, persist=True)
    render_portfolio(snap)

    results: list[bool] = []
    print(f"  {'─'*64}\n  CHECKS\n  {'─'*64}")

    # ── 1. Risk % per position ───────────────────────────────────────────────
    results.append(_check("Risk % (stop distance) on every position",
                          all(p.stop_pct > 0 for p in snap.positions) if snap.positions else True,
                          f"{[(p.symbol, p.stop_pct) for p in snap.positions]}"))

    # ── 2. Inverse to stop distance (pure PositionSizer test) ────────────────
    ps = PositionSizer()
    narrow, wide = ps.size_by_risk(40, 5.0), ps.size_by_risk(40, 10.0)
    results.append(_check("Risk-based sizing inverse to stop distance",
                          wide < narrow,
                          f"conv40: 5% stop→{narrow}%  vs  10% stop→{wide}%"))

    # ── 3. Risk budget honored for uncapped positions ────────────────────────
    uncapped = [p for p in snap.positions
                if p.allocation == p.desired_size and p.desired_size < MAX_POSITION_SIZE - 1e-6]
    ok3 = all(abs(p.risk_contribution - ps.risk_budget(p.conviction)) < 0.06 for p in uncapped)
    results.append(_check("Risk budget honored (uncapped ≈ conviction-scaled risk)",
                          ok3, f"{len(uncapped)} uncapped checked"))

    # ── 4. Portfolio risk metric bounded ─────────────────────────────────────
    pr = snap.metrics.get("portfolio_risk_pct")
    bound = (len(snap.positions) or 1) * RISK_PER_TRADE_MAX + EPS
    results.append(_check("Portfolio risk metric generated & bounded",
                          pr is not None and 0 <= pr <= bound,
                          f"portfolio_risk={pr}%  (≤ {bound:.1f}%)"))

    # ── 5. Limits regression ─────────────────────────────────────────────────
    sec = {}
    clu = {}
    for p in snap.positions:
        sec[p.sector] = sec.get(p.sector, 0) + p.allocation
        clu[p.cluster_id] = clu.get(p.cluster_id, 0) + p.allocation
    ok5 = (snap.deployed <= REGIME_EXPOSURE.get(snap.regime, 50) + EPS
           and max(sec.values(), default=0) <= MAX_SECTOR_EXPOSURE + EPS
           and max(clu.values(), default=0) <= MAX_CLUSTER_EXPOSURE + EPS)
    results.append(_check("Regime / sector / cluster limits respected (regression)", ok5,
                          f"deployed {snap.deployed}% / exp {snap.recommended_exposure}%"))

    # ── 6. Deterministic ─────────────────────────────────────────────────────
    snap2 = PortfolioConstructor().construct(res.act_snap, res.regime, res.feed.data)
    sig1 = [(p.ticker, p.allocation, p.stop_pct, p.risk_contribution) for p in snap.positions]
    sig2 = [(p.ticker, p.allocation, p.stop_pct, p.risk_contribution) for p in snap2.positions]
    results.append(_check("Deterministic across repeated runs", sig1 == sig2))

    # ── 7. Risk % in exports ─────────────────────────────────────────────────
    paths = export_portfolio(snap)
    j = paths["json"].read_text(encoding="utf-8")
    c = paths["csv"].read_text(encoding="utf-8")
    md = paths["md"].read_text(encoding="utf-8")
    ok7 = ("risk_pct" in j and "risk_pct" in c and "Risk %" in md)
    results.append(_check("Risk % present in JSON + CSV + MD exports", ok7))

    # ── 8. Exports generated ─────────────────────────────────────────────────
    results.append(_check("Exports generated",
                          all(p.exists() and p.stat().st_size > 0 for p in paths.values())))

    print(f"\n  {'='*64}")
    passed = sum(results)
    ok = passed == len(results)
    print(f"  {(GREEN if ok else RED)}{passed}/{len(results)} checks passed{RST}")
    print(f"  {'='*64}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
