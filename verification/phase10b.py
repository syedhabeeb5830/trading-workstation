"""
verification/phase10b.py — Phase 10B (Portfolio Lifecycle) acceptance checks
============================================================================
Proves the lifecycle engine (must pass 8/8) + prints a sample review.

  1. Every holding receives a lifecycle status.
  2. Theme exposure cap enforced.
  3. Action labels generated.
  4. Rotation recommendations generated.
  5. Exit logic fires correctly.
  6. Regime-aware behavior changes.
  7. Deterministic.
  8. JSON/CSV/MD exports generated.
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
from portfolio.lifecycle_engine import (                                       # noqa: E402
    LifecycleManager, ThemeDiversifier, RotationAnalyzer, PositionState,
    Holding, action_label, load_holdings_from_portfolio, export_review,
    MAX_THEME_EXPOSURE)

logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")
GREEN, RED, DIM, BOLD, RST = "\033[92m", "\033[91m", "\033[2m", "\033[1m", "\033[0m"
VALID_STATUS = {"HOLD", "ADD", "REDUCE", "EXIT", "ROTATE"}
VALID_ACTIONS = {"BUY_NOW", "BUY_PULLBACK", "WAIT_BREAKOUT", "WAIT_PULLBACK", "AVOID", "WAIT"}


def _check(label, ok, detail=""):
    mark = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))
    return ok


def _price(screen, ticker):
    df = screen.feed.data.get(ticker)
    return float(df["Close"].iloc[-1]) if df is not None and not df.empty else 0.0


def main() -> int:
    print(f"\n  {'='*64}\n  PHASE 10B — PORTFOLIO LIFECYCLE\n  {'='*64}")
    res = build_screen(period="1y", persist=False)
    mgr = LifecycleManager()
    results: list[bool] = []

    # Real holdings from the last constructed portfolio.
    holdings = load_holdings_from_portfolio()
    if not holdings:   # fallback: synthesise from the top actionable names
        holdings = [Holding(r.ticker, r.symbol, r.sector, 8.0, _price(res, r.ticker))
                    for r in res.act_snap.top_n(3)]
    snap = mgr.review(holdings, res, persist=True)

    from portfolio.lifecycle_engine import render_review
    render_review(snap)
    print(f"  {BOLD}══ EXAMPLE JSON ══{RST}\n{snap.export_json()[:700]}")
    print(f"\n  {'─'*64}\n  CHECKS\n  {'─'*64}")

    # ── 1. Every holding gets a status ───────────────────────────────────────
    results.append(_check("Every holding receives a lifecycle status",
                          len(snap.rows) == len(holdings)
                          and all(r.status in VALID_STATUS for r in snap.rows),
                          f"{len(snap.rows)} holdings: "
                          + ", ".join(f"{r.symbol}={r.status}" for r in snap.rows)))

    # ── 2. Theme cap enforced (45% in one theme → flagged) ───────────────────
    cg = [r for r in res.act_snap.rows if r.sector == "Capital Goods"][:3]
    concentrated = [Holding(r.ticker, r.symbol, r.sector, 15.0, _price(res, r.ticker))
                    for r in cg]
    td = ThemeDiversifier()
    over = td.over_cap(concentrated)
    results.append(_check(f"Theme exposure cap enforced (> {MAX_THEME_EXPOSURE}%)",
                          len(over) >= 1,
                          f"45% Capital Goods → over-cap themes: {over}"))

    # ── 3. Action labels ─────────────────────────────────────────────────────
    results.append(_check("Action labels generated",
                          all(r.action in VALID_ACTIONS for r in snap.rows)
                          and action_label("PULLBACK_SETUP", "WATCHLIST") == "BUY_PULLBACK"
                          and action_label("BREAKOUT_SETUP", "ACTION_NOW") == "BUY_NOW"
                          and action_label("x", "EXTENDED") == "WAIT_PULLBACK",
                          ", ".join(sorted({r.action for r in snap.rows}))))

    # ── 4. Rotation recommendations ──────────────────────────────────────────
    cands = mgr._candidates_by_theme(res, held=set())
    theme = next((th for th, cs in cands.items()
                  if cs and max(c.conviction for c in cs) > 50), None)
    rotated = False
    if theme:
        best = max(c.conviction for c in cands[theme])
        weak = PositionState(
            ticker="WEAK.NS", symbol="WEAK", sector="x", theme=theme, allocation=5,
            entry_price=100, stop_price=None, current_price=100, pnl_pct=0,
            rs_status="NEUTRAL", rs_bucket="TOP_50", grade="B", composite_score=60,
            actionability_score=60, rr=2.0, classification="WATCHLIST",
            entry_context="PULLBACK_SETUP", action="BUY_PULLBACK",
            conviction=best - 30, extension_pct=0, in_universe=True,
            stop_hit=False, trend_break=False)
        rotated = RotationAnalyzer().check(weak, cands, "BULL") is not None
    results.append(_check("Rotation recommendations generated", rotated,
                          f"theme={theme}, best vs weak Δ>{15}"))

    # ── 5. Exit logic fires (stop hit) ───────────────────────────────────────
    t = holdings[0].ticker
    px = _price(res, t)
    stopped = Holding(t, holdings[0].symbol, holdings[0].sector, 8.0, px, stop_price=px * 1.5)
    exit_snap = mgr.review([stopped], res)
    results.append(_check("Exit logic fires correctly (stop hit → EXIT)",
                          exit_snap.rows[0].status == "EXIT" and "Stop hit" in exit_snap.rows[0].reason,
                          exit_snap.rows[0].reason))

    # ── 6. Regime-aware behavior changes ─────────────────────────────────────
    nl = next((r for r in res.act_snap.rows if r.rs_status == "NEUTRAL"
               and r.classification != "AVOID"), None)
    if nl is None:
        nl = res.act_snap.rows[len(res.act_snap.rows) // 2]
    nlh = [Holding(nl.ticker, nl.symbol, nl.sector, 8.0, _price(res, nl.ticker))]
    bull = mgr.review(nlh, res, regime_override="STRONG_BULL").rows[0].status
    bear = mgr.review(nlh, res, regime_override="STRONG_BEAR").rows[0].status
    results.append(_check("Regime-aware behavior changes",
                          bear == "EXIT" and bull != "EXIT",
                          f"{nl.symbol} ({nl.rs_status}): STRONG_BULL={bull} · STRONG_BEAR={bear}"))

    # ── 7. Deterministic ─────────────────────────────────────────────────────
    s2 = mgr.review(holdings, res)
    sig1 = [(r.ticker, r.status, r.action, r.rotate_to) for r in snap.rows]
    sig2 = [(r.ticker, r.status, r.action, r.rotate_to) for r in s2.rows]
    results.append(_check("Deterministic across repeated runs", sig1 == sig2))

    # ── 8. Exports ───────────────────────────────────────────────────────────
    paths = export_review(snap)
    results.append(_check("JSON/CSV/MD exports generated",
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
