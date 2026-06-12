"""
verification/release_candidate.py — RC1 Hardening: full end-to-end system audit
================================================================================
A FREEZE-phase audit. Adds no features and changes no logic — it validates that
every subsystem of the dynamic screener works, surfaces inconsistencies with
explicit explanations, scans data integrity, and times the pipeline.

Writes:
    reports/data_integrity_report.md
    reports/performance_report.md

Run:  python verification/release_candidate.py
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from scanner.universe_builder import build_universe                            # noqa: E402
from scanner.data_feed import get_ohlcv                                        # noqa: E402
from scanner.sector_engine import SectorRanker                                 # noqa: E402
from scanner.relative_strength_engine import RelativeStrengthRanker, fetch_nifty  # noqa: E402
from scanner.composite_engine import CompositeRanker                           # noqa: E402
from scanner.actionability_engine import ActionabilityRanker                   # noqa: E402
from scanner.regime_engine import RegimeClassifier                             # noqa: E402
from scanner.adaptive_scoring import AdaptiveScorer                            # noqa: E402
from screen.screen_runner import build_screen, export_reports                  # noqa: E402
from portfolio.portfolio_engine import (                                       # noqa: E402
    PortfolioConstructor, REGIME_EXPOSURE, MAX_SECTOR_EXPOSURE, MAX_CLUSTER_EXPOSURE)
from portfolio.lifecycle_engine import LifecycleManager, resolve_holdings      # noqa: E402

logging.basicConfig(level=logging.ERROR, format="  [%(levelname)s] %(message)s")
GREEN, RED, YEL, DIM, BOLD, RST = (
    "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[1m", "\033[0m")
REPORTS = Path("reports")


def _check(label, ok, detail=""):
    mark = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))
    return ok


def _note(label, detail):
    print(f"  [{YEL}NOTE{RST}] {label}  {DIM}{detail}{RST}")


def main() -> int:
    print(f"\n  {'='*70}\n  RELEASE CANDIDATE (RC1) — FULL SYSTEM AUDIT\n  {'='*70}\n")
    results: list[bool] = []
    timings: dict[str, float] = {}

    # ── Build the pipeline with per-stage timing ─────────────────────────────
    t = time.perf_counter(); uni = build_universe(); timings["universe"] = time.perf_counter() - t
    t = time.perf_counter(); feed = get_ohlcv(uni.tickers, period="1y", min_bars=30)
    timings["data_fetch"] = time.perf_counter() - t
    sm = uni.sector_map
    t = time.perf_counter(); nifty = fetch_nifty("1y"); timings["nifty"] = time.perf_counter() - t
    t = time.perf_counter(); sector = SectorRanker(sm, feed.data, uni.source).rank()
    timings["sector"] = time.perf_counter() - t
    t = time.perf_counter(); rs = RelativeStrengthRanker(feed.data, sm, nifty, sector).rank()
    timings["relative_strength"] = time.perf_counter() - t
    t = time.perf_counter(); comp = CompositeRanker(feed.data, sm, sector, rs).rank()
    timings["composite"] = time.perf_counter() - t
    t = time.perf_counter(); act = ActionabilityRanker(comp, feed.data).rank()
    timings["actionability"] = time.perf_counter() - t
    t = time.perf_counter()
    regime = RegimeClassifier().analyze(
        ohlcv=feed.data, index_dfs={"Nifty50": nifty}, sector_snapshot=sector,
        rs_snapshot=rs, composite_snapshot=comp, actionability_snapshot=act)
    timings["regime"] = time.perf_counter() - t
    t = time.perf_counter(); adaptive = AdaptiveScorer().score(comp, regime)
    timings["adaptive"] = time.perf_counter() - t

    # ════════════════════════════════════════════════════════════════════════
    print(f"  {BOLD}PART 1 — SUBSYSTEM AUDIT{RST}")
    # ════════════════════════════════════════════════════════════════════════
    results.append(_check("Screen: universe generated", uni.symbol_count >= 400,
                          f"{uni.symbol_count} symbols ({uni.source})"))
    results.append(_check("Screen: OHLCV coverage ≥ 95%", feed.coverage >= 0.95,
                          f"{feed.coverage*100:.0f}%"))
    results.append(_check("Screen: sector ranking generated", len(sector.rows) > 0,
                          f"{len(sector.rows)} sectors"))
    results.append(_check("Screen: RS ranking generated", len(rs.ranked) > 0,
                          f"{len(rs.ranked)} ranked"))
    results.append(_check("Screen: composite ranking generated", len(comp.rows) > 0,
                          f"{len(comp.rows)} ranked"))
    results.append(_check("Screen: actionability ranking generated", len(act.rows) > 0,
                          f"{len(act.rows)} ranked"))
    results.append(_check("Screen: regime generated", regime.regime != "",
                          f"{regime.regime} ({regime.regime_score})"))
    results.append(_check("Screen: adaptive generated", len(adaptive.rows) > 0))
    paths = export_reports(build_screen(period="1y", persist=False))
    results.append(_check("Screen: exports created",
                          all(p.exists() for p in paths.values())))

    # ── Portfolio ────────────────────────────────────────────────────────────
    # Reuse a lightweight ScreenResult-like for the constructor.
    res = build_screen(period="1y", persist=False)
    t = time.perf_counter()
    port = PortfolioConstructor().construct(res.act_snap, res.regime, res.feed.data)
    timings["portfolio_construct"] = time.perf_counter() - t
    sec_exp, clu_exp = {}, {}
    for p in port.positions:
        sec_exp[p.sector] = sec_exp.get(p.sector, 0) + p.allocation
        clu_exp[p.cluster_id] = clu_exp.get(p.cluster_id, 0) + p.allocation
    results.append(_check("Portfolio: allocations within exposure + limits",
                          port.deployed <= port.recommended_exposure + 0.5
                          and max(sec_exp.values(), default=0) <= MAX_SECTOR_EXPOSURE + 0.5
                          and max(clu_exp.values(), default=0) <= MAX_CLUSTER_EXPOSURE + 0.5,
                          f"deployed {port.deployed}% ≤ {port.recommended_exposure}%"))

    # ── Review (holdings source must be unambiguous) ─────────────────────────
    holdings, source = resolve_holdings(res.universe.sector_map)
    t = time.perf_counter()
    review = LifecycleManager().review(holdings, res)
    timings["portfolio_review"] = time.perf_counter() - t
    unambiguous = source.startswith("ZERODHA KITE") or source.startswith("SIMULATED")
    results.append(_check("Review: holdings source unambiguous", unambiguous, source))
    results.append(_check("Review: every holding has a status",
                          all(r.status in {"HOLD", "ADD", "REDUCE", "EXIT", "ROTATE"}
                              for r in review.rows),
                          f"{len(review.rows)} holdings"))

    # ── Validation engines present ───────────────────────────────────────────
    from analytics import edge_validation, rolling_validation  # noqa: F401
    results.append(_check("Validation engines importable (edge + rolling)", True))

    # ════════════════════════════════════════════════════════════════════════
    print(f"\n  {BOLD}PART 2 — CONSISTENCY (explicit explanations, not failures){RST}")
    # ════════════════════════════════════════════════════════════════════════
    # Old --today regime (SMA gate) vs new --screen regime (multi-dimensional).
    try:
        from scanner.scanner import get_market_regime
        from config.config import CONFIG
        old_regime = get_market_regime(CONFIG)["regime"]
    except Exception as exc:
        old_regime = f"error({exc})"
    _note("Regime engines differ BY DESIGN",
          f"--today (old SMA gate)={old_regime}  vs  --screen (multi-dim)={regime.regime}")
    print(f"        {DIM}--today hard-blocks new longs in BEAR; --screen surfaces ranked "
          f"opportunities and lets the\n        portfolio engine cap exposure "
          f"({REGIME_EXPOSURE.get(regime.regime)}% in {regime.regime}). Both correct; "
          f"different systems.{RST}")

    # Ranking provenance: portfolio tickers must come from actionability.
    act_tickers = {r.ticker for r in res.act_snap.rows}
    port_from_act = all(p.ticker in act_tickers for p in port.positions)
    results.append(_check("Portfolio positions originate from actionability ranking",
                          port_from_act, f"{len(port.positions)} positions checked"))

    # Theme cap is a LIFECYCLE control, NOT a construction control (documented).
    _note("Theme cap (40%) applies to REVIEW/ADD, not CONSTRUCTION",
          "construction enforces sector(30%)+cluster(20%); theme is a known gap → backlog")

    # ════════════════════════════════════════════════════════════════════════
    print(f"\n  {BOLD}PART 3 — DATA INTEGRITY{RST}")
    # ════════════════════════════════════════════════════════════════════════
    issues: list[str] = []
    if uni.df["sector"].isna().any() or (uni.df["sector"] == "").any():
        issues.append("universe: blank sectors")
    if uni.df["symbol"].duplicated().any():
        issues.append("universe: duplicate symbols")
    empty = [t for t, d in feed.data.items() if d is None or d.empty]
    if empty:
        issues.append(f"ohlcv: {len(empty)} empty frames")
    rranks = [r.rank for r in rs.ranked]
    if len(rranks) != len(set(rranks)):
        issues.append("RS: duplicate ranks")
    cranks = [r.rank for r in comp.rows]
    if len(cranks) != len(set(cranks)):
        issues.append("composite: duplicate ranks")
    if any(not r.classification for r in act.rows):
        issues.append("actionability: missing classification")
    if any(r.score is None for r in comp.rows):
        issues.append("composite: missing scores")
    results.append(_check("Data integrity: no NaN/dup/missing anomalies",
                          len(issues) == 0, "; ".join(issues) if issues else "clean"))

    _write_integrity_report(uni, feed, sector, rs, comp, act, issues)
    _write_performance_report(timings, feed)
    print(f"  {DIM}wrote reports/data_integrity_report.md + reports/performance_report.md{RST}")

    print(f"\n  {'='*70}")
    passed = sum(results)
    ok = passed == len(results)
    print(f"  {(GREEN if ok else RED)}RC1 AUDIT: {passed}/{len(results)} checks passed{RST}")
    print(f"  {'='*70}\n")
    return 0 if ok else 1


def _write_integrity_report(uni, feed, sector, rs, comp, act, issues):
    REPORTS.mkdir(parents=True, exist_ok=True)
    lines = [f"# Data Integrity Report — {date.today().isoformat()}", "",
             f"- Universe: **{uni.symbol_count}** symbols, source `{uni.source}`, "
             f"{uni.df['sector'].nunique()} sectors",
             f"- OHLCV coverage: **{feed.coverage*100:.1f}%** "
             f"({feed.succeeded}/{feed.requested}); failed: {len(feed.failed)}",
             f"- Sectors ranked: {len(sector.rows)} · RS ranked: {len(rs.ranked)} · "
             f"Composite: {len(comp.rows)} · Actionability: {len(act.rows)}", "",
             "## Anomalies", ""]
    lines += [f"- ⚠ {i}" for i in issues] or ["- None — all checks clean ✅"]
    (REPORTS / "data_integrity_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_performance_report(timings: dict, feed):
    REPORTS.mkdir(parents=True, exist_ok=True)
    total = sum(timings.values())
    rows = sorted(timings.items(), key=lambda kv: -kv[1])
    lines = [f"# Performance Report — {date.today().isoformat()}", "",
             f"Pipeline wall time (cached data feed): **{total:.1f}s**  "
             f"(fetch source: {feed.source})", "",
             "| Stage | Seconds | % |", "|-------|--------:|--:|"]
    for k, v in rows:
        lines.append(f"| {k} | {v:.2f} | {v/total*100:.0f}% |")
    lines += ["", "## Observations (identify-only, no optimization performed)",
              "- **Data fetch dominates** when the daily cache is cold; cached re-runs are seconds.",
              "- Each CLI (`--screen`, `--portfolio`, `--review-portfolio`) calls `build_screen` "
              "independently → the full pipeline is **recomputed per command** in a session. "
              "A shared in-process cache is a future optimization (NOT done — freeze).",
              "- Walk-forward validation re-runs the whole stack per as-of date (intentionally)."]
    (REPORTS / "performance_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
