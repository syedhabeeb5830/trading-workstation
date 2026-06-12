"""
analytics/edge_validation.py — Edge Validation & Performance Analytics (Phase 8)
================================================================================
Up to Phase 7 the system answers "what looks strong today?". This phase answers
the only question that matters for a quant process: "does the screener actually
have edge?" — measured from OUTCOMES, not opinions.

    What happened AFTER the screen?   (not: what looked good on the screen?)

It validates, with forward returns:
  • Grade buckets (A+ should beat D, monotonically)
  • RS classification (MARKET_LEADER should beat LAGGARD)
  • Sector leadership (top sectors should beat bottom)
  • Actionability (ACTION_NOW should beat AVOID) — the headline test of Phase 5
  • Regime (which regimes are profitable)
  • Adaptive vs Composite (does Phase-7 re-weighting add alpha?)
  • A Top-5 ACTION_NOW equal-weight portfolio (the screener's report card)
…then fuses them into a single EDGE_SCORE / 100.

Cold-start note
---------------
Forward returns need history that has *aged*. The persisted weekly snapshots
(Journal/*_history) are the long-term source, but on day one they carry no
future. So this engine also supports WALK-FORWARD REPLAY: it re-runs the
existing Phase 2-7 engines as-of past dates (slicing the OHLCV we already have)
and measures the real bars that followed. No engine logic is rebuilt — the
rankers are simply replayed through time.

Analyzers are independent. EdgeValidationEngine orchestrates; EdgeValidationSnapshot
stores, persists, and reports.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from scanner.sector_engine import SectorRanker
from scanner.relative_strength_engine import RelativeStrengthRanker, fetch_nifty
from scanner.composite_engine import CompositeRanker
from scanner.actionability_engine import ActionabilityRanker
from scanner.regime_engine import RegimeClassifier
from scanner.adaptive_scoring import AdaptiveScorer

_log = logging.getLogger(__name__)

HORIZONS = [5, 10, 20, 60]
PRIMARY_HORIZON = "20"          # swing edge measured at 20 trading days

_HISTORY_DIR = Path("Journal/edge_validation")
_REPORTS_DIR = Path("reports")


# ─────────────────────────────────────────────────────────────────────────────
# RECORD
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ScreenRecord:
    """One (screen_date, stock) observation captured by a screen, plus the
    forward returns that followed it."""
    screen_date:         str
    ticker:              str
    sector:              str
    composite_score:     float
    grade:               str
    composite_rank:      int
    rs_status:           str
    classification:      str
    actionability_score: float
    adaptive_score:      float
    regime:              str
    sector_rank:         Optional[int]
    n_sectors:           int
    leader_weeks:        Optional[int] = None
    fwd:                 dict[str, float] = field(default_factory=dict)


def _avg(xs: list[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 2) if xs else None


# ─────────────────────────────────────────────────────────────────────────────
# FORWARD RETURN ENGINE
# ─────────────────────────────────────────────────────────────────────────────
class ForwardReturnEngine:
    """Attaches N-day forward returns to each record using the real bars after
    its screen_date."""

    def attach(self, records: list[ScreenRecord], ohlcv: dict[str, pd.DataFrame],
               horizons: list[int] = None) -> list[ScreenRecord]:
        horizons = horizons or HORIZONS
        for r in records:
            df = ohlcv.get(r.ticker)
            r.fwd = {}
            if df is None or df.empty:
                continue
            sd = pd.Timestamp(r.screen_date)
            pos = int(df.index.searchsorted(sd, side="right")) - 1
            if pos < 0 or pos >= len(df):
                continue
            base = float(df["Close"].iloc[pos])
            if base <= 0:
                continue
            for h in horizons:
                j = pos + h
                if j < len(df):
                    r.fwd[str(h)] = round((float(df["Close"].iloc[j]) / base - 1) * 100, 2)
        return records


# ─────────────────────────────────────────────────────────────────────────────
# BUCKET PERFORMANCE (grade / RS / sector tier)
# ─────────────────────────────────────────────────────────────────────────────
class BucketPerformanceAnalyzer:
    GRADE_ORDER = ["A+", "A", "B+", "B", "C", "D"]
    RS_ORDER = ["MARKET_LEADER", "SECTOR_LEADER", "EMERGING_LEADER", "NEUTRAL", "LAGGARD"]

    def by_grade(self, records, horizon=PRIMARY_HORIZON) -> dict:
        return self._bucket(records, lambda r: r.grade, self.GRADE_ORDER, horizon)

    def by_rs(self, records, horizon=PRIMARY_HORIZON) -> dict:
        return self._bucket(records, lambda r: r.rs_status, self.RS_ORDER, horizon)

    def by_sector_tier(self, records, horizon=PRIMARY_HORIZON) -> dict:
        def tier(r):
            if r.sector_rank is None or not r.n_sectors:
                return "Unknown"
            if r.sector_rank <= 5:
                return "Top 5 sectors"
            if r.sector_rank > r.n_sectors - 5:
                return "Bottom sectors"
            return "Middle sectors"
        return self._bucket(records, tier,
                            ["Top 5 sectors", "Middle sectors", "Bottom sectors"], horizon)

    @staticmethod
    def _bucket(records, keyfn, order, horizon) -> dict:
        groups: dict[str, list[float]] = {}
        for r in records:
            k = keyfn(r)
            groups.setdefault(k, []).append(r.fwd.get(horizon))
        out = {}
        for k in order:
            if k in groups:
                out[k] = {"avg_return": _avg(groups[k]),
                          "n": len([x for x in groups[k] if x is not None])}
        # include any keys not in the canonical order
        for k, v in groups.items():
            if k not in out:
                out[k] = {"avg_return": _avg(v),
                          "n": len([x for x in v if x is not None])}
        return out


# ─────────────────────────────────────────────────────────────────────────────
# ACTIONABILITY PERFORMANCE
# ─────────────────────────────────────────────────────────────────────────────
class ActionabilityAnalyzer:
    ORDER = ["ACTION_NOW", "WATCHLIST", "EXTENDED", "AVOID"]

    def by_classification(self, records, horizon=PRIMARY_HORIZON) -> dict:
        groups: dict[str, list[float]] = {}
        for r in records:
            groups.setdefault(r.classification, []).append(r.fwd.get(horizon))
        return {k: {"avg_return": _avg(groups[k]),
                    "win_rate": _win_rate(groups[k]),
                    "n": len([x for x in groups[k] if x is not None])}
                for k in self.ORDER if k in groups}


# ─────────────────────────────────────────────────────────────────────────────
# REGIME PERFORMANCE
# ─────────────────────────────────────────────────────────────────────────────
def _win_rate(xs) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return round(sum(1 for x in xs if x > 0) / len(xs), 3) if xs else None


def _profit_factor(xs) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    gains = sum(x for x in xs if x > 0)
    losses = -sum(x for x in xs if x < 0)
    if losses == 0:
        return round(gains, 2) if gains else None
    return round(gains / losses, 2)


class RegimePerformanceAnalyzer:
    def by_regime(self, records, horizon=PRIMARY_HORIZON) -> dict:
        groups: dict[str, list[float]] = {}
        for r in records:
            groups.setdefault(r.regime, []).append(r.fwd.get(horizon))
        return {k: {"avg_return": _avg(v), "win_rate": _win_rate(v),
                    "profit_factor": _profit_factor(v),
                    "n": len([x for x in v if x is not None])}
                for k, v in sorted(groups.items())}


# ─────────────────────────────────────────────────────────────────────────────
# SNAPSHOT
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class EdgeValidationSnapshot:
    generated_at:    str
    horizon:         str
    n_records:       int
    n_screen_dates:  int
    grade_perf:      dict
    rs_perf:         dict
    sector_perf:     dict
    action_perf:     dict
    regime_perf:     dict
    adaptive_vs_composite: dict
    portfolio:       dict
    edge_score:      float
    edge_components: dict
    verdict:         str

    def to_dict(self) -> dict:
        return asdict(self)

    def export_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def save(self, history_dir: Path = _HISTORY_DIR) -> Path:
        history_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.generated_at[:10]
        path = history_dir / f"edge_{stamp}.json"
        path.write_text(self.export_json())
        # append a one-line CSV history
        csv = history_dir / "edge_history.csv"
        try:
            new = pd.DataFrame([{
                "date": stamp, "horizon": self.horizon, "edge_score": self.edge_score,
                "verdict": self.verdict, "n_records": self.n_records,
                **{f"edge_{k}": v for k, v in self.edge_components.items()},
            }])
            if csv.exists():
                old = pd.read_csv(csv)
                old = old[old["date"] != stamp]
                new = pd.concat([old, new], ignore_index=True)
            new.to_csv(csv, index=False, encoding="utf-8")
        except Exception as exc:
            _log.warning("edge history csv: %s", exc)
        return path

    def to_markdown(self) -> str:
        def tbl(d, cols):
            lines = ["| " + " | ".join(cols) + " |",
                     "|" + "|".join(["---"] * len(cols)) + "|"]
            for k, v in d.items():
                lines.append(f"| {k} | " + " | ".join(
                    f"{v.get(c) if v.get(c) is not None else '—'}" for c in cols[1:]) + " |")
            return "\n".join(lines)

        L = [f"# Edge Validation Report — {self.generated_at[:10]}", "",
             f"**EDGE SCORE: {self.edge_score:.0f} / 100 — {self.verdict}**", "",
             f"Horizon: {self.horizon}D · {self.n_records} observations across "
             f"{self.n_screen_dates} screen dates", "",
             "## Grade performance", tbl(self.grade_perf, ["grade", "avg_return", "n"]), "",
             "## Relative-strength performance", tbl(self.rs_perf, ["rs_status", "avg_return", "n"]), "",
             "## Sector-tier performance", tbl(self.sector_perf, ["tier", "avg_return", "n"]), "",
             "## Actionability performance", tbl(self.action_perf, ["class", "avg_return", "win_rate", "n"]), "",
             "## Regime performance", tbl(self.regime_perf, ["regime", "avg_return", "win_rate", "profit_factor", "n"]), "",
             "## Adaptive vs Composite",
             f"- Composite Top-20 alpha: {self.adaptive_vs_composite.get('composite_alpha')}%",
             f"- Adaptive Top-20 alpha: {self.adaptive_vs_composite.get('adaptive_alpha')}%",
             f"- Adaptive improvement: {self.adaptive_vs_composite.get('improvement')}%", "",
             "## Top-5 ACTION_NOW portfolio",
             f"- Avg trade return: {self.portfolio.get('avg_return')}%",
             f"- Win rate: {self.portfolio.get('win_rate')}",
             f"- Sharpe: {self.portfolio.get('sharpe')}  ·  Sortino: {self.portfolio.get('sortino')}",
             f"- Max drawdown: {self.portfolio.get('max_drawdown')}%",
             f"- Trades: {self.portfolio.get('n_trades')}  ({self.portfolio.get('basis')})", "",
             "## Edge score components",
             tbl({k: {"points": v} for k, v in self.edge_components.items()}, ["component", "points"]), ""]
        return "\n".join(L) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────
class EdgeValidationEngine:
    def __init__(self):
        self.fwd = ForwardReturnEngine()
        self.bucket = BucketPerformanceAnalyzer()
        self.action = ActionabilityAnalyzer()
        self.regime = RegimePerformanceAnalyzer()

    def run(self, records: list[ScreenRecord], ohlcv: dict[str, pd.DataFrame],
            horizon: str = PRIMARY_HORIZON, persist: bool = False) -> EdgeValidationSnapshot:
        # Ensure forward returns are attached.
        if records and not records[0].fwd:
            self.fwd.attach(records, ohlcv)

        grade = self.bucket.by_grade(records, horizon)
        rs = self.bucket.by_rs(records, horizon)
        sector = self.bucket.by_sector_tier(records, horizon)
        action = self.action.by_classification(records, horizon)
        regime = self.regime.by_regime(records, horizon)
        avc = self._adaptive_vs_composite(records, horizon)
        portfolio = self._portfolio(records, horizon)
        score, comps = self._edge_score(grade, action, regime, avc, portfolio, horizon)
        verdict = ("STRONG EDGE" if score >= 80 else "PROMISING EDGE" if score >= 60
                   else "WEAK EDGE" if score >= 40 else "NO EDGE")

        n_dates = len({r.screen_date for r in records})
        snap = EdgeValidationSnapshot(
            datetime.now().isoformat(timespec="seconds"), horizon, len(records), n_dates,
            grade, rs, sector, action, regime, avc, portfolio, round(score, 1), comps, verdict)
        if persist:
            snap.save()
        return snap

    # ── Adaptive vs Composite alpha ──────────────────────────────────────────
    def _adaptive_vs_composite(self, records, horizon, top=20) -> dict:
        comp_sel, adp_sel, universe = [], [], []
        by_date: dict[str, list[ScreenRecord]] = {}
        for r in records:
            by_date.setdefault(r.screen_date, []).append(r)
        for recs in by_date.values():
            universe += [r.fwd.get(horizon) for r in recs]
            comp_top = sorted(recs, key=lambda r: r.composite_rank)[:top]
            adp_top = sorted(recs, key=lambda r: -r.adaptive_score)[:top]
            comp_sel += [r.fwd.get(horizon) for r in comp_top]
            adp_sel += [r.fwd.get(horizon) for r in adp_top]
        base = _avg(universe) or 0.0
        comp_alpha = round((_avg(comp_sel) or 0.0) - base, 2)
        adp_alpha = round((_avg(adp_sel) or 0.0) - base, 2)
        return {"composite_alpha": comp_alpha, "adaptive_alpha": adp_alpha,
                "improvement": round(adp_alpha - comp_alpha, 2),
                "universe_avg": round(base, 2)}

    # ── Top-5 ACTION_NOW portfolio ───────────────────────────────────────────
    def _portfolio(self, records, horizon) -> dict:
        by_date: dict[str, list[ScreenRecord]] = {}
        for r in records:
            by_date.setdefault(r.screen_date, []).append(r)

        per_date, basis = [], "Top-5 ACTION_NOW"
        for d in sorted(by_date):
            recs = by_date[d]
            picks = [r for r in recs if r.classification == "ACTION_NOW"]
            if len(picks) < 1:                       # fallback when no ACTION_NOW
                picks = recs
                basis = "Top-5 by actionability (no ACTION_NOW)"
            picks = sorted(picks, key=lambda r: -r.actionability_score)[:5]
            rets = [r.fwd.get(horizon) for r in picks if r.fwd.get(horizon) is not None]
            if rets:
                per_date.append(sum(rets) / len(rets))

        if not per_date:
            return {"avg_return": None, "win_rate": None, "sharpe": None,
                    "sortino": None, "max_drawdown": None, "n_trades": 0, "basis": basis}

        avg = sum(per_date) / len(per_date)
        sd = statistics.pstdev(per_date) if len(per_date) > 1 else 0.0
        downside = [x for x in per_date if x < 0]
        dsd = statistics.pstdev(downside) if len(downside) > 1 else 0.0
        # Equity curve (compounded) for max drawdown.
        eq, peak, mdd = 1.0, 1.0, 0.0
        for r in per_date:
            eq *= (1 + r / 100)
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1)
        return {"avg_return": round(avg, 2), "win_rate": _win_rate(per_date),
                "sharpe": round(avg / sd, 2) if sd else None,
                "sortino": round(avg / dsd, 2) if dsd else None,
                "max_drawdown": round(mdd * 100, 2), "n_trades": len(per_date),
                "basis": basis}

    # ── EDGE SCORE ───────────────────────────────────────────────────────────
    def _edge_score(self, grade, action, regime, avc, portfolio, horizon) -> tuple[float, dict]:
        # 1) Bucket separation (25): monotonic grade returns + A+ vs D spread.
        order = [g for g in ["A+", "A", "B+", "B", "C", "D"] if g in grade]
        rets = [grade[g]["avg_return"] for g in order if grade[g]["avg_return"] is not None]
        mono = 0.0
        if len(rets) >= 2:
            steps = sum(1 for i in range(len(rets) - 1) if rets[i] >= rets[i + 1])
            mono = steps / (len(rets) - 1)
        spread = (rets[0] - rets[-1]) if len(rets) >= 2 else 0.0
        bucket_sep = 25 * (0.5 * mono + 0.5 * _clamp(spread / 10))

        # 2) Actionability edge (25): ACTION_NOW (or WATCHLIST) minus AVOID.
        a_now = (action.get("ACTION_NOW") or action.get("WATCHLIST") or {}).get("avg_return")
        a_avoid = (action.get("AVOID") or {}).get("avg_return")
        if a_now is not None and a_avoid is not None:
            action_edge = 25 * _clamp((a_now - a_avoid) / 8)
        else:
            action_edge = 12.5  # neutral when buckets unavailable
        # 3) Regime edge (15): overall win rate proxy.
        wrs = [v["win_rate"] for v in regime.values() if v["win_rate"] is not None]
        overall_wr = sum(wrs) / len(wrs) if wrs else 0.5
        regime_edge = 15 * _clamp((overall_wr - 0.40) / 0.30)

        # 4) Adaptive improvement (15): centred at 7.5, +4% → 15.
        regime_imp = 15 * _clamp(0.5 + avc["improvement"] / 8)

        # 5) Portfolio performance (20).
        pavg = portfolio.get("avg_return")
        port = 20 * _clamp((pavg or 0.0) / 10)

        comps = {"bucket_separation": round(bucket_sep, 1),
                 "actionability_edge": round(action_edge, 1),
                 "regime_edge": round(regime_edge, 1),
                 "adaptive_improvement": round(regime_imp, 1),
                 "portfolio_performance": round(port, 1)}
        return sum(comps.values()), comps


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ─────────────────────────────────────────────────────────────────────────────
# WALK-FORWARD REPLAY (bootstrap when live history hasn't aged)
# ─────────────────────────────────────────────────────────────────────────────
def _replay_as_of(ohlcv_full, sector_map, nifty_full, as_of: pd.Timestamp,
                  min_bars: int = 60):
    """Replay the full Phase 2-7 stack as-of `as_of`. Returns (records, rs_snapshot)."""
    sliced = {t: df[df.index <= as_of] for t, df in ohlcv_full.items()}
    sliced = {t: d for t, d in sliced.items() if len(d) >= min_bars}
    if not sliced:
        return [], None
    nifty = nifty_full[nifty_full.index <= as_of] if nifty_full is not None else None

    sector_snap = SectorRanker(sector_map, sliced).rank()
    rs_snap = RelativeStrengthRanker(sliced, sector_map, nifty, sector_snap).rank()
    comp_snap = CompositeRanker(sliced, sector_map, sector_snap, rs_snap).rank()
    act_snap = ActionabilityRanker(comp_snap, sliced).rank()
    regime = RegimeClassifier().analyze(
        ohlcv=sliced, index_dfs={"Nifty50": nifty}, sector_snapshot=sector_snap,
        rs_snapshot=rs_snap, composite_snapshot=comp_snap, actionability_snapshot=act_snap)
    adaptive = AdaptiveScorer().score(comp_snap, regime)

    n_sectors = len(sector_snap.rows)
    sd = as_of.date().isoformat()
    recs = []
    for cr in comp_snap.rows:
        a = act_snap.get(cr.ticker)
        ad = adaptive.get(cr.ticker)
        recs.append(ScreenRecord(
            screen_date=sd, ticker=cr.ticker, sector=cr.sector,
            composite_score=cr.score, grade=cr.grade, composite_rank=cr.rank,
            rs_status=cr.rs_status,
            classification=a.classification if a else "AVOID",
            actionability_score=a.actionability_score if a else 0.0,
            adaptive_score=ad.adaptive_score if ad else cr.score,
            regime=regime.regime, sector_rank=cr.sector_rank, n_sectors=n_sectors))
    return recs, rs_snap


def backfill_records(ohlcv_full, sector_map, nifty_full,
                     offsets=(220, 190, 160, 130, 100, 80)) -> list[ScreenRecord]:
    """Replay the screener as-of several past dates → records with real forward bars."""
    if nifty_full is None or len(nifty_full) < max(offsets) + 5:
        # fall back to a liquid stock's index for date selection
        ref = max(ohlcv_full.values(), key=len)
        cal = ref.index
    else:
        cal = nifty_full.index
    recs: list[ScreenRecord] = []
    for off in offsets:
        if off >= len(cal):
            continue
        as_of = cal[-off]
        day_recs, _ = _replay_as_of(ohlcv_full, sector_map, nifty_full, as_of)
        recs += day_recs
    ForwardReturnEngine().attach(recs, ohlcv_full)
    return recs


# ─────────────────────────────────────────────────────────────────────────────
# TOP-LEVEL RUN (data assembly + report + persistence)  — used by CLI + verify
# ─────────────────────────────────────────────────────────────────────────────
def render_edge_report(snap: EdgeValidationSnapshot) -> None:
    G, R, Y, B, D, RST = ("\033[92m", "\033[91m", "\033[93m", "\033[1m", "\033[2m", "\033[0m")
    vc = {"STRONG EDGE": G, "PROMISING EDGE": G, "WEAK EDGE": Y, "NO EDGE": R}.get(snap.verdict, "")

    def line(label, val, pct=True):
        if val is None:
            return f"{label}: —"
        col = G if val > 0 else (R if val < 0 else "")
        return f"{label}: {col}{val:+.1f}{'%' if pct else ''}{RST}"

    print(f"\n  {B}{'═'*55}{RST}")
    print(f"  {B}  EDGE VALIDATION REPORT{RST}")
    print(f"  {B}{'═'*55}{RST}")
    print(f"\n  {B}EDGE SCORE: {vc}{snap.edge_score:.0f} / 100{RST}  "
          f"({snap.n_records} obs · {snap.n_screen_dates} screens · {snap.horizon}D)")
    comps = "  ".join(f"{k.split('_')[0]} {v:.0f}" for k, v in snap.edge_components.items())
    print(f"  {D}{comps}{RST}")

    print(f"\n  {B}Grade returns{RST}")
    for g, v in snap.grade_perf.items():
        print(f"    {line(f'{g:>2}', v['avg_return'])}   {D}(n={v['n']}){RST}")

    print(f"\n  {B}Actionability{RST}")
    for c, v in snap.action_perf.items():
        print(f"    {line(f'{c:11s}', v['avg_return'])}   {D}win {v['win_rate']}  n={v['n']}{RST}")

    print(f"\n  {B}RS classification{RST}")
    for c, v in snap.rs_perf.items():
        print(f"    {line(f'{c:16s}', v['avg_return'])}   {D}n={v['n']}{RST}")

    print(f"\n  {B}Adaptive vs Composite{RST}")
    print(f"    Composite Top-20 alpha: {snap.adaptive_vs_composite['composite_alpha']:+.1f}%")
    print(f"    Adaptive  Top-20 alpha: {snap.adaptive_vs_composite['adaptive_alpha']:+.1f}%")
    imp = snap.adaptive_vs_composite["improvement"]
    print(f"    {(G if imp>0 else R)}Adaptive {'beats' if imp>0 else 'trails'} Composite by "
          f"{imp:+.1f}%{RST}")

    p = snap.portfolio
    print(f"\n  {B}Top-5 Portfolio{RST}  {D}({p['basis']}){RST}")
    print(f"    Avg trade {line('', p['avg_return']).strip(': ')}  ·  win {p['win_rate']}  ·  "
          f"Sharpe {p['sharpe']}  ·  Sortino {p['sortino']}  ·  MaxDD {p['max_drawdown']}%  "
          f"·  trades {p['n_trades']}")

    print(f"\n  {B}Verdict: {vc}{snap.verdict}{RST}")
    print(f"  {B}{'═'*55}{RST}\n")


def run_edge_validation(period: str = "2y", sample: int = 0,
                        offsets=(220, 190, 160, 130, 100, 80),
                        horizon: str = PRIMARY_HORIZON, persist: bool = True,
                        export: bool = True) -> tuple[EdgeValidationSnapshot, list[ScreenRecord]]:
    from scanner.universe_builder import build_universe
    from scanner.data_feed import get_ohlcv

    uni = build_universe()
    tickers = uni.tickers[:sample] if sample else uni.tickers
    sub = uni.df[uni.df["ticker"].isin(tickers)]
    sector_map = dict(zip(sub["ticker"], sub["sector"]))

    feed = get_ohlcv(tickers, period=period, min_bars=60)
    nifty = fetch_nifty(period=period)

    records = backfill_records(feed.data, sector_map, nifty, offsets=offsets)
    snap = EdgeValidationEngine().run(records, feed.data, horizon=horizon, persist=persist)

    if export:
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = date.today().isoformat()
        (_REPORTS_DIR / f"edge_report_{stamp}.json").write_text(snap.export_json(), encoding="utf-8")
        (_REPORTS_DIR / f"edge_report_{stamp}.md").write_text(snap.to_markdown(), encoding="utf-8")
    return snap, records
