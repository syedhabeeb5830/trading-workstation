"""
screen/screen_runner.py — Swing-Trading Cockpit (Phase 5)
=========================================================
Orchestrates the full Phase 1-5 pipeline and renders a trader-facing dashboard:

    1. Market Regime
    2. Top Sectors
    3. Top 20 Candidate Board
    4. Top 5 Actionable Trades

Then exports the board to reports/screen_<date>.{json,csv,md} and persists a
weekly snapshot to Journal/actionability_history/.

Entry point: `run_screen()` — invoked by `python run.py --screen`.
`build_screen()` returns every snapshot for reuse (verification, future phases).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

from scanner.universe_builder import build_universe, Universe
from scanner.data_feed import get_ohlcv, FetchResult
from scanner.sector_engine import SectorRanker, SectorSnapshot
from scanner.relative_strength_engine import (
    RelativeStrengthRanker, RelativeStrengthSnapshot, fetch_nifty)
from scanner.composite_engine import CompositeRanker, CompositeSnapshot
from scanner.actionability_engine import ActionabilityRanker, ActionabilitySnapshot
from scanner.regime_engine import RegimeClassifier, RegimeSnapshot
from scanner.adaptive_scoring import AdaptiveScorer, AdaptiveSnapshot
from scanner.leader_persistence import LeaderPersistenceTracker
from portfolio.lifecycle_engine import action_label

_REPORTS_DIR = Path("reports")

G, R, Y, B, D, RST = ("\033[92m", "\033[91m", "\033[93m", "\033[1m", "\033[2m", "\033[0m")


@dataclass
class ScreenResult:
    universe:     Universe
    feed:         FetchResult
    regime:       RegimeSnapshot
    sector_snap:  SectorSnapshot
    rs_snap:      RelativeStrengthSnapshot
    comp_snap:    CompositeSnapshot
    act_snap:     ActionabilitySnapshot
    adaptive_snap: AdaptiveSnapshot
    leader_weeks: dict


# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE
# ─────────────────────────────────────────────────────────────────────────────
def build_screen(period: str = "1y", force_refresh: bool = False,
                 persist: bool = True) -> ScreenResult:
    uni = build_universe(force_refresh=force_refresh)
    sector_map = uni.sector_map

    feed = get_ohlcv(uni.tickers, period=period, force_refresh=force_refresh, min_bars=30)
    nifty = fetch_nifty(period=period)                       # ^NSEI
    nifty500 = fetch_nifty(period=period, ticker="^CRSLDX")  # Nifty 500 (optional)
    vix = fetch_nifty(period=period, ticker="^INDIAVIX")     # India VIX (optional)

    sector_snap = SectorRanker(sector_map, feed.data, uni.source).rank(persist=persist)
    rs_snap = RelativeStrengthRanker(feed.data, sector_map, nifty, sector_snap).rank(persist=persist)
    comp_snap = CompositeRanker(feed.data, sector_map, sector_snap, rs_snap).rank(persist=persist)
    act_snap = ActionabilityRanker(comp_snap, feed.data).rank(persist=persist)

    index_dfs = {"Nifty50": nifty}
    if nifty500 is not None:
        index_dfs["Nifty500"] = nifty500
    regime = RegimeClassifier().analyze(
        ohlcv=feed.data, index_dfs=index_dfs, sector_snapshot=sector_snap,
        rs_snapshot=rs_snap, composite_snapshot=comp_snap,
        actionability_snapshot=act_snap, vix_df=vix, persist=persist)

    adaptive_snap = AdaptiveScorer().score(comp_snap, regime, persist=persist)
    leader_weeks = LeaderPersistenceTracker().from_history(rs_snap)

    return ScreenResult(uni, feed, regime, sector_snap, rs_snap, comp_snap,
                        act_snap, adaptive_snap, leader_weeks)


# ─────────────────────────────────────────────────────────────────────────────
# COCKPIT RENDER
# ─────────────────────────────────────────────────────────────────────────────
def _regime_color(reg: str) -> str:
    return {"STRONG_BULL": G, "BULL": G, "NEUTRAL": Y, "RANGE": Y,
            "WEAK_BEAR": R, "STRONG_BEAR": R, "VOLATILE": R}.get(reg, "")


def _class_color(c: str) -> str:
    return {"ACTION_NOW": G, "WATCHLIST": Y, "EXTENDED": D, "AVOID": R}.get(c, "")


def render_cockpit(res: ScreenResult) -> None:
    today = date.today().isoformat()
    print(f"\n{B}{'═'*100}{RST}")
    print(f"{B}  SWING TRADING COCKPIT  ·  {today}  ·  "
          f"universe={res.universe.symbol_count} ({res.universe.source})  "
          f"data={res.feed.coverage*100:.0f}%{RST}")
    print(f"{B}{'═'*100}{RST}")

    # 1) Market regime (multi-dimensional)
    reg = res.regime
    rc = _regime_color(reg.regime)
    print(f"\n  {B}{'═'*60}{RST}")
    print(f"  {B}  MARKET REGIME{RST}     {rc}{B}{reg.regime}{RST}     "
          f"{B}{reg.regime_score:.1f}{RST} / 100")
    print(f"  {B}{'═'*60}{RST}")
    print(f"  {D}Trend {reg.trend_score:.0f}  ·  Breadth {reg.breadth_score:.0f}  ·  "
          f"Leadership {reg.leadership_score:.0f}  ·  Participation "
          f"{reg.participation_score:.0f}  ·  Volatility {reg.volatility_score:.0f} "
          f"({reg.volatility_state}){RST}")
    for line in reg.summary:
        print(f"    {D}• {line}{RST}")

    # 2) Top sectors
    print(f"\n  {B}TOP SECTORS{RST}")
    print(f"  {D}{'#':>2}  {'SECTOR':30s}  {'SCORE':>5}  {'GR':>3}  STATUS{RST}")
    for r in res.sector_snap.rows[:5]:
        gc = G if r.grade in ("A+", "A") else ""
        print(f"  {r.rank:>2}  {r.sector[:30]:30s}  {gc}{r.score:>5.1f}{RST}  "
              f"{gc}{r.grade:>3}{RST}  {r.status}")

    # 2b) Leadership-exhaustion warnings (display only — NOT a score).
    # Phase-8 validation showed 8+ week TOP_10 leaders UNDERPERFORM (mean-reversion),
    # so persistence is surfaced as a caution, never as positive conviction.
    lw = res.leader_weeks or {}
    mature = sorted(((t, w) for t, w in lw.items() if w >= 8), key=lambda kv: -kv[1])
    maturing = sorted(((t, w) for t, w in lw.items() if 4 <= w < 8), key=lambda kv: -kv[1])
    if mature or maturing:
        print(f"\n  {B}LEADERSHIP EXHAUSTION{RST}  "
              f"{D}(8+ wks in TOP_10 historically mean-revert — caution, not a score){RST}")
        for t, w in mature[:8]:
            sym = t[:-3] if t.endswith(".NS") else t
            print(f"    {Y}⚠ {sym}: {w} consecutive weeks TOP_10 — momentum mature{RST}")
        if maturing:
            cells = ", ".join((t[:-3] if t.endswith('.NS') else t) + f" {w}w"
                              for t, w in maturing[:8])
            print(f"    {D}maturing (4-7 wks): {cells}{RST}")

    # 3) Top 20 candidate board
    print(f"\n  {B}TOP 20 CANDIDATE BOARD{RST}")
    print(f"  {D}{'#':>2}  {'TICKER':12s}  {'COMP':>5}  {'ACT':>5}  {'GR':>3}  "
          f"{'CLASS':10s}  {'RR':>4}  {'ENTRY ZONE':21s}  {'STOP':>9}  DRIVERS{RST}")
    print(f"  {D}{'─'*150}{RST}")
    for r in res.act_snap.top_n(20):
        cc = _class_color(r.classification)
        gc = G if r.grade in ("A+", "A") else (Y if r.grade in ("B+", "B") else "")
        drv = " · ".join(r.drivers[:3])
        print(f"  {r.rank:>2}  {r.symbol[:12]:12s}  {r.composite_score:>5.1f}  "
              f"{r.actionability_score:>5.1f}  {gc}{r.grade:>3}{RST}  "
              f"{cc}{r.classification:10s}{RST}  {r.rr_ratio:>4.1f}  "
              f"{r.entry_zone:21s}  {r.stop_zone:>9}  {drv}")

    # 4) Top 5 actionable trades
    print(f"\n  {B}{'═'*60}{RST}")
    print(f"  {B}  TOP 5 ACTIONABLE SWING TRADES{RST}")
    print(f"  {B}{'═'*60}{RST}")
    action = res.act_snap.action_now(5)
    if not action:
        cd = res.act_snap.classification_distribution()
        print(f"\n  {Y}No ACTION_NOW setups today.{RST}  "
              f"{D}(EXTENDED={cd['EXTENDED']}, WATCHLIST={cd['WATCHLIST']} — "
              f"strong names await better entries){RST}")
    for i, r in enumerate(action, 1):
        act_lbl = action_label(r.entry_context, r.classification)
        print(f"\n  {B}{i}. {r.symbol}{RST}   {_class_color(r.classification)}"
              f"{r.classification}{RST}   {G}{B}[{act_lbl}]{RST}")
        print(f"     Composite {r.composite_score:.1f}  ·  Actionability "
              f"{r.actionability_score:.1f}  ·  RR {r.rr_ratio:.1f}  ·  Grade {r.grade}")
        print(f"     Entry {r.entry_zone}   Stop {r.stop_zone}   Target {r.target_zone}")
        print(f"     {D}{' · '.join(r.drivers[:4])}{RST}")
        if r.warnings:
            print(f"     {Y}⚠ {' · '.join(r.warnings)}{RST}")

    # 5) Adaptive (regime-aware) scoring
    _render_adaptive(res.adaptive_snap)
    print()


def _render_adaptive(adp: AdaptiveSnapshot) -> None:
    print(f"\n  {B}{'═'*60}{RST}")
    print(f"  {B}  ADAPTIVE SCORING{RST}   regime {B}{adp.regime}{RST}")
    print(f"  {B}{'═'*60}{RST}")
    wt = "  ".join(f"{_LABEL_SHORT.get(k,k).upper()} {int(v)}"
                   for k, v in sorted(adp.weights.items(), key=lambda kv: -kv[1]))
    print(f"  {D}Weight profile:  {wt}{RST}")

    # Adaptive candidate board (top 10) — composite vs adaptive vs change.
    print(f"\n  {D}{'TICKER':12s}  {'COMP':>5}  {'ADAPT':>5}  {'Δ':>6}  {'A#':>3}  (C#){RST}")
    for r in adp.top_n(10):
        col = G if r.score_change > 0 else (R if r.score_change < 0 else "")
        print(f"  {r.symbol[:12]:12s}  {r.composite_score:>5.1f}  "
              f"{r.adaptive_score:>5.1f}  {col}{r.score_change:>+6.1f}{RST}  "
              f"{r.adaptive_rank:>3}  (#{r.composite_rank})")

    proms, dems = adp.top_promotions(5), adp.top_demotions(5)
    print(f"\n  {B}Top Promotions{RST}")
    for r in proms:
        print(f"  {G}{r.symbol:12s} {r.score_change:>+5.1f}{RST}  "
              f"{D}{r.reason[0] if r.reason else ''}{RST}")
    print(f"\n  {B}Top Demotions{RST}")
    for r in dems:
        print(f"  {R}{r.symbol:12s} {r.score_change:>+5.1f}{RST}  "
              f"{D}{r.reason[0] if r.reason else ''}{RST}")


_LABEL_SHORT = {"rs": "RS", "sector": "SECTOR", "breakout": "BREAKOUT",
                "trend": "TREND", "liquidity": "LIQUIDITY", "atr": "ATR",
                "freshness": "FRESHNESS"}


# ─────────────────────────────────────────────────────────────────────────────
# EXPORTS
# ─────────────────────────────────────────────────────────────────────────────
def export_reports(res: ScreenResult, reports_dir: Path = _REPORTS_DIR) -> dict[str, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    paths = {
        "json": reports_dir / f"screen_{stamp}.json",
        "csv":  reports_dir / f"screen_{stamp}.csv",
        "md":   reports_dir / f"screen_{stamp}.md",
    }
    paths["json"].write_text(res.act_snap.export_json(), encoding="utf-8")
    paths["csv"].write_text(res.act_snap.to_csv(), encoding="utf-8")
    paths["md"].write_text(res.act_snap.to_markdown(), encoding="utf-8")
    return paths


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
def run_screen(period: str = "1y", force_refresh: bool = False) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(f"\n  {D}Building swing screen (universe → sector → RS → composite → "
          f"actionability)...{RST}")
    res = build_screen(period=period, force_refresh=force_refresh, persist=True)
    render_cockpit(res)
    paths = export_reports(res)
    print(f"  {D}Exports: {paths['json']} · {paths['csv']} · {paths['md']}{RST}\n")
    return 0
