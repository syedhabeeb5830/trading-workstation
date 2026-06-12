"""
analytics/rolling_validation.py — Rolling Edge Validation Framework (Improvement 2)
===================================================================================
Phase 8 proved the *machinery* but on only 4 screen dates — far too few to trust
any verdict. This is the permanent statistical framework: replay the screener on
a WEEKLY cadence across a multi-year window (≈100 Fridays) and evaluate forward
returns at 5/10/20/60 days with significance testing.

It answers, with t-stats instead of vibes:
  • Does A+ actually outperform D?              (grade edge)
  • Do MARKET_LEADERs beat LAGGARDs?            (RS edge)
  • Does ACTION_NOW beat AVOID?                 (actionability edge — Phase-8 red flag)
  • Does adaptive weighting beat composite?     (Phase-7 value)

Reuses the Phase-8 engines (no logic rebuilt): `_replay_as_of` runs the full
Phase 2-7 stack as-of each Friday; `EdgeValidationEngine` produces the bucket
tables and EDGE_SCORE; this module adds weekly date generation, multi-horizon
aggregation, and the significance layer on top.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from analytics.edge_validation import (
    ScreenRecord, ForwardReturnEngine, EdgeValidationEngine, _replay_as_of,
    HORIZONS, _avg, _HISTORY_DIR, _REPORTS_DIR,
)
from scanner.leader_persistence import LeaderPersistenceTracker

_log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# STATISTICS (no scipy dependency)
# ─────────────────────────────────────────────────────────────────────────────
def _clean(xs) -> list[float]:
    return [x for x in xs if x is not None]


def _welch_t(a, b) -> Optional[float]:
    a, b = _clean(a), _clean(b)
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = statistics.variance(a), statistics.variance(b)
    se = math.sqrt(va / len(a) + vb / len(b))
    if se == 0:
        return None
    return round((statistics.mean(a) - statistics.mean(b)) / se, 2)


def _one_sample_t(xs) -> Optional[float]:
    xs = _clean(xs)
    if len(xs) < 2:
        return None
    sd = statistics.stdev(xs)
    if sd == 0:
        return None
    return round(statistics.mean(xs) / (sd / math.sqrt(len(xs))), 2)


def _significant(t: Optional[float]) -> bool:
    return t is not None and abs(t) >= 1.96       # ≈ p < 0.05 (two-sided, large n)


# ─────────────────────────────────────────────────────────────────────────────
# WEEKLY AS-OF GENERATION
# ─────────────────────────────────────────────────────────────────────────────
def _weekly_asofs(cal: pd.DatetimeIndex, years: float, every_weeks: int,
                  min_prior: int = 200, min_forward: int = 5) -> list[pd.Timestamp]:
    """Last trading day of each week, within the most-recent `years`, with enough
    prior history for indicators and at least a little forward room."""
    weekly: dict[tuple, pd.Timestamp] = {}
    for ts in cal:
        iso = ts.isocalendar()
        weekly[(iso[0], iso[1])] = ts          # latest day seen in that ISO week
    fridays = sorted(weekly.values())

    cutoff = cal[-1] - pd.Timedelta(days=int(years * 365))
    pos = {ts: i for i, ts in enumerate(cal)}
    out = []
    for ts in fridays:
        if ts < cutoff:
            continue
        i = pos.get(ts)
        if i is None or i < min_prior or i > len(cal) - 1 - min_forward:
            continue
        out.append(ts)
    return out[::every_weeks]


# ─────────────────────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class QuestionResult:
    question:    str
    top_label:   str
    bottom_label: str
    mean_top:    Optional[float]
    mean_bottom: Optional[float]
    diff:        Optional[float]
    t_stat:      Optional[float]
    significant: bool
    n_top:       int
    n_bottom:    int
    verdict:     str


@dataclass
class RollingValidationReport:
    generated_at:   str
    n_dates:        int
    n_obs:          int
    date_range:     str
    regimes:        dict
    horizons:       list[str]
    edge_score:     float
    edge_components: dict
    verdict:        str
    grade_perf:     dict          # primary-horizon table (20D)
    action_perf:    dict
    rs_perf:        dict
    questions:      dict          # horizon -> {grade, rs, actionability, adaptive}

    def to_dict(self) -> dict:
        return asdict(self)

    def export_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def save(self, history_dir: Path = _HISTORY_DIR) -> Path:
        history_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.generated_at[:10]
        path = history_dir / f"rolling_{stamp}.json"
        path.write_text(self.export_json())
        return path

    def to_markdown(self) -> str:
        L = [f"# Rolling Edge Validation — {self.generated_at[:10]}", "",
             f"**EDGE SCORE: {self.edge_score:.0f} / 100 — {self.verdict}**", "",
             f"{self.n_dates} weekly screens · {self.n_obs} observations · "
             f"{self.date_range}", "",
             "Regimes covered: " + ", ".join(f"{k}={v}" for k, v in self.regimes.items()),
             "", "## Key questions (statistical significance)", ""]
        for h in self.horizons:
            L.append(f"### {h}D horizon")
            L.append("| Question | Top | Bottom | Diff | t | Significant | Verdict |")
            L.append("|---|--:|--:|--:|--:|:--:|:--|")
            for q in self.questions[h].values():
                L.append(f"| {q['question']} | {q['mean_top']} | {q['mean_bottom']} | "
                         f"{q['diff']} | {q['t_stat']} | {'YES' if q['significant'] else 'no'} "
                         f"| {q['verdict']} |")
            L.append("")
        return "\n".join(L) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────
def _question(records, horizon, label, q_text, top_pred, bot_pred,
              top_label, bot_label) -> QuestionResult:
    top = [r.fwd.get(horizon) for r in records if top_pred(r)]
    bot = [r.fwd.get(horizon) for r in records if bot_pred(r)]
    mt, mb = _avg(top), _avg(bot)
    diff = round(mt - mb, 2) if (mt is not None and mb is not None) else None
    t = _welch_t(top, bot)
    sig = _significant(t)
    if diff is None:
        verdict = "insufficient data"
    elif diff > 0 and sig:
        verdict = "WORKS (significant +)"
    elif diff > 0:
        verdict = "positive but not significant"
    elif diff <= 0 and sig:
        verdict = "INVERTED (significant −)"
    else:
        verdict = "no edge"
    return QuestionResult(q_text, top_label, bot_label, mt, mb, diff, t, sig,
                          len(_clean(top)), len(_clean(bot)), verdict)


def _adaptive_question(records, horizon, top=20) -> QuestionResult:
    """Paired per-date test: adaptive Top-N forward vs composite Top-N forward."""
    by_date: dict[str, list[ScreenRecord]] = {}
    for r in records:
        by_date.setdefault(r.screen_date, []).append(r)
    diffs = []
    for recs in by_date.values():
        comp_top = sorted(recs, key=lambda r: r.composite_rank)[:top]
        adp_top = sorted(recs, key=lambda r: -r.adaptive_score)[:top]
        cm = _avg([r.fwd.get(horizon) for r in comp_top])
        am = _avg([r.fwd.get(horizon) for r in adp_top])
        if cm is not None and am is not None:
            diffs.append(am - cm)
    if not diffs:
        return QuestionResult("Adaptive beats Composite", "adaptive", "composite",
                              None, None, None, None, False, 0, 0, "insufficient data")
    mean_diff = round(statistics.mean(diffs), 2)
    t = _one_sample_t(diffs)
    sig = _significant(t)
    verdict = ("WORKS (significant +)" if mean_diff > 0 and sig else
               "positive but not significant" if mean_diff > 0 else
               "INVERTED (significant −)" if sig else "no edge")
    return QuestionResult("Adaptive beats Composite (Top-20)", "adaptive", "composite",
                          None, None, mean_diff, t, sig, len(diffs), len(diffs), verdict)


def _questions_for_horizon(records, horizon) -> dict:
    return {
        "grade": asdict(_question(
            records, horizon, "grade", "A-tier (A+/A) beats D-tier (C/D)",
            lambda r: r.grade in ("A+", "A"), lambda r: r.grade in ("C", "D"),
            "A+/A", "C/D")),
        "rs": asdict(_question(
            records, horizon, "rs", "MARKET_LEADER beats LAGGARD",
            lambda r: r.rs_status == "MARKET_LEADER", lambda r: r.rs_status == "LAGGARD",
            "MARKET_LEADER", "LAGGARD")),
        "actionability": asdict(_question(
            records, horizon, "actionability", "ACTION_NOW beats AVOID",
            lambda r: r.classification == "ACTION_NOW", lambda r: r.classification == "AVOID",
            "ACTION_NOW", "AVOID")),
        "persistence": asdict(_question(
            records, horizon, "persistence", "Persistent leaders (≥8w) beat transient (≤2w)",
            lambda r: r.leader_weeks is not None and r.leader_weeks >= 8,
            lambda r: r.leader_weeks is not None and 1 <= r.leader_weeks <= 2,
            "≥8w TOP_10", "≤2w TOP_10")),
        "adaptive": asdict(_adaptive_question(records, horizon)),
    }


# ─────────────────────────────────────────────────────────────────────────────
# TOP-LEVEL RUN
# ─────────────────────────────────────────────────────────────────────────────
def generate_rolling_records(ohlcv_full, sector_map, nifty_full,
                             years: float, every_weeks: int,
                             progress: bool = True) -> tuple[list[ScreenRecord], list]:
    cal = (nifty_full.index if (nifty_full is not None and len(nifty_full) > 250)
           else max(ohlcv_full.values(), key=len).index)
    asofs = _weekly_asofs(cal, years, every_weeks)
    tracker = LeaderPersistenceTracker()
    rs_snaps = []
    recs: list[ScreenRecord] = []
    for n, ts in enumerate(asofs, 1):
        day_recs, rs_snap = _replay_as_of(ohlcv_full, sector_map, nifty_full, ts)
        if rs_snap is not None:
            rs_snaps.append(rs_snap)
            streaks = tracker.streaks(rs_snaps)       # leader_weeks as-of this date
            for r in day_recs:
                r.leader_weeks = streaks.get(r.ticker, 0)
        recs += day_recs
        if progress and (n % 10 == 0 or n == len(asofs)):
            print(f"    …replayed {n}/{len(asofs)} weekly screens", flush=True)
    ForwardReturnEngine().attach(recs, ohlcv_full, HORIZONS)
    return recs, asofs


def run_rolling_validation(period: str = "5y", years: float = 2.0,
                           every_weeks: int = 1, sample: int = 0,
                           horizons=("5", "10", "20", "60"),
                           persist: bool = True, export: bool = True
                           ) -> tuple[RollingValidationReport, list[ScreenRecord]]:
    from scanner.universe_builder import build_universe
    from scanner.data_feed import get_ohlcv
    from analytics.edge_validation import fetch_nifty

    uni = build_universe()
    tickers = uni.tickers[:sample] if sample else uni.tickers
    sub = uni.df[uni.df["ticker"].isin(tickers)]
    sector_map = dict(zip(sub["ticker"], sub["sector"]))

    feed = get_ohlcv(tickers, period=period, min_bars=60)
    nifty = fetch_nifty(period=period)

    records, asofs = generate_rolling_records(feed.data, sector_map, nifty,
                                              years, every_weeks)

    # Headline edge score on the primary horizon via the Phase-8 engine.
    eng = EdgeValidationEngine()
    snap = eng.run(records, feed.data, horizon="20", persist=False)

    questions = {h: _questions_for_horizon(records, h) for h in horizons}
    regimes: dict[str, int] = {}
    dates = sorted({r.screen_date for r in records})
    for r in records:
        regimes[r.regime] = regimes.get(r.regime, 0) + 1
    drange = f"{dates[0]} → {dates[-1]}" if dates else "—"

    report = RollingValidationReport(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        n_dates=len(dates), n_obs=len(records), date_range=drange,
        regimes=dict(sorted(regimes.items(), key=lambda kv: -kv[1])),
        horizons=list(horizons), edge_score=snap.edge_score,
        edge_components=snap.edge_components, verdict=snap.verdict,
        grade_perf=snap.grade_perf, action_perf=snap.action_perf, rs_perf=snap.rs_perf,
        questions=questions)

    if persist:
        report.save()
    if export:
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = date.today().isoformat()
        (_REPORTS_DIR / f"rolling_report_{stamp}.json").write_text(report.export_json(), encoding="utf-8")
        (_REPORTS_DIR / f"rolling_report_{stamp}.md").write_text(report.to_markdown(), encoding="utf-8")
    return report, records


# ─────────────────────────────────────────────────────────────────────────────
# RENDER
# ─────────────────────────────────────────────────────────────────────────────
def render_rolling_report(rep: RollingValidationReport) -> None:
    G, R, Y, B, D, RST = ("\033[92m", "\033[91m", "\033[93m", "\033[1m", "\033[2m", "\033[0m")
    vc = {"STRONG EDGE": G, "PROMISING EDGE": G, "WEAK EDGE": Y, "NO EDGE": R}.get(rep.verdict, "")
    print(f"\n  {B}{'═'*64}{RST}")
    print(f"  {B}  ROLLING EDGE VALIDATION{RST}")
    print(f"  {B}{'═'*64}{RST}")
    print(f"\n  {B}EDGE SCORE: {vc}{rep.edge_score:.0f} / 100{RST}  "
          f"{D}({rep.n_dates} weekly screens · {rep.n_obs} obs · {rep.date_range}){RST}")
    print(f"  {D}Regimes: " + "  ".join(f"{k}={v}" for k, v in rep.regimes.items()) + RST)

    for h in rep.horizons:
        print(f"\n  {B}{h}D horizon{RST}")
        for q in rep.questions[h].values():
            sig = q["significant"]
            ok = q["diff"] is not None and q["diff"] > 0
            col = G if (ok and sig) else (Y if ok else R)
            diff = f"{q['diff']:+.2f}%" if q["diff"] is not None else "—"
            t = f"t={q['t_stat']}" if q["t_stat"] is not None else "t=—"
            print(f"    {col}{q['question']:38s}{RST}  Δ {diff:>8}  {t:>8}  "
                  f"{D}n={q['n_top']}/{q['n_bottom']}{RST}  {col}{q['verdict']}{RST}")
    print(f"\n  {B}Verdict: {vc}{rep.verdict}{RST}")
    print(f"  {B}{'═'*64}{RST}\n")
