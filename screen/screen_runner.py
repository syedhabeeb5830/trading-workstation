"""
screen/screen_runner.py — Swing-Trading Cockpit (Phase 5)
=========================================================
Orchestrates the full Phase 1-5 pipeline and renders a DECISION-FIRST cockpit:

    1. Data quality → Market Regime → Top Sectors
    2. Top 10 Candidate Board (top 20 with --research)
    3. ZERO-DECISION TRADE PLAN:
         ▶ COMMITTEE DECISION — exactly one of BUY NOW / BUY ON PULLBACK /
           BUY ON BREAKOUT / WAIT FOR TRIGGER / STAY IN CASH (with proof)
         ② confirmed BUY orders (entry/stop/T1/T2/qty/risk, calibrated)
         ③ CONDITIONAL GTT orders for armed leaders (trigger/limit/stop/qty)
         MANAGE — one mechanical exit ruleset (no discretion left)

Explicit triggers (2026-07-02 deadlock fix): a trade fires on ACTION_NOW, a
confirmed breakout bar (close > pivot on ≥1.2x volume), or a confirmed pullback
reclaim bar (up-day close above prior high) — all computed on the last COMPLETE
session. RR never gates anything (proven non-predictive OOS).

Then exports the board to reports/screen_<date>.{json,csv,md} and persists a
weekly snapshot to Journal/actionability_history/. Research tables (adaptive
promotions/demotions, extended monitor list) render only with --research.

Entry point: `run_screen()` — invoked by `python run.py --screen`.
`build_screen()` returns every snapshot for reuse (verification, future phases).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from scanner.universe_builder import build_universe, Universe
from scanner.data_feed import get_ohlcv, FetchResult
from scanner.sector_engine import SectorRanker, SectorSnapshot
from scanner.relative_strength_engine import (
    RelativeStrengthRanker, RelativeStrengthSnapshot, fetch_nifty, fetch_benchmark)
from scanner.composite_engine import CompositeRanker, CompositeSnapshot
from scanner.actionability_engine import ActionabilityRanker, ActionabilitySnapshot
from scanner.regime_engine import RegimeClassifier, RegimeSnapshot
from scanner.adaptive_scoring import AdaptiveScorer, AdaptiveSnapshot
from scanner.leader_persistence import LeaderPersistenceTracker
from portfolio.lifecycle_engine import action_label
from portfolio.lifecycle_states import (is_signal_triggered as _lc_triggered,
                                        is_armed as _lc_armed,
                                        decide as _lc_decide)

_REPORTS_DIR = Path("reports")

G, R, Y, B, D, RST = ("\033[92m", "\033[91m", "\033[93m", "\033[1m", "\033[2m", "\033[0m")


class BenchmarkUnavailableError(RuntimeError):
    """Raised by build_screen when the market benchmark (^NSEI) is unavailable from
    BOTH live download AND cache. Fail-loud per the data-resilience policy: a screen
    without the benchmark cannot produce a trustworthy regime/leadership read, so we
    refuse rather than emit a false-bearish board. Pass allow_degraded=True to force
    sector-relative fallback instead."""

    def __init__(self, benches: dict):
        self.benches = benches or {}
        super().__init__("Market benchmark ^NSEI unavailable (live + cache both failed)")


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
    data_quality: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE
# ─────────────────────────────────────────────────────────────────────────────
def build_screen(period: str = "1y", force_refresh: bool = False,
                 persist: bool = True, allow_degraded: bool = False) -> ScreenResult:
    uni = build_universe(force_refresh=force_refresh)
    sector_map = uni.sector_map

    feed = get_ohlcv(uni.tickers, period=period, force_refresh=force_refresh, min_bars=30)
    nifty_res    = fetch_benchmark(period=period, ticker="^NSEI")       # market benchmark (required)
    nifty500_res = fetch_benchmark(period=period, ticker="^CRSLDX")     # Nifty 500 (optional)
    vix_res      = fetch_benchmark(period=period, ticker="^INDIAVIX")   # India VIX (optional)

    # FAIL LOUD: the market benchmark is required for a trustworthy regime/leadership
    # read. If it is gone from BOTH live and cache, refuse rather than emit a
    # false-bearish board. allow_degraded=True forces sector-relative fallback instead.
    if nifty_res.status == "missing" and not allow_degraded:
        raise BenchmarkUnavailableError(
            {"Nifty50": nifty_res.status, "Nifty500": nifty500_res.status,
             "IndiaVIX": vix_res.status})

    nifty, nifty500, vix = nifty_res.df, nifty500_res.df, vix_res.df

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

    data_quality = _assess_data_quality(feed, nifty_res, nifty500_res, vix_res, rs_snap)
    return ScreenResult(uni, feed, regime, sector_snap, rs_snap, comp_snap,
                        act_snap, adaptive_snap, leader_weeks, data_quality)


def _assess_data_quality(feed, nifty_res, nifty500_res, vix_res, rs_snap) -> dict:
    """Summarise stock + benchmark coverage into a single HEALTHY/DEGRADED/FAILED
    verdict for the cockpit. Reporting only — changes no scores."""
    benches = {"Nifty50": nifty_res.status, "Nifty500": nifty500_res.status,
               "IndiaVIX": vix_res.status}
    nifty_missing = nifty_res.status == "missing"          # the critical benchmark
    any_degraded = any(s != "live" for s in benches.values()) \
        or rs_snap.benchmark_status != "OK"
    overall = ("FAILED" if nifty_missing
               else "DEGRADED" if any_degraded else "HEALTHY")
    return {
        "stocks_pct": round(feed.coverage * 100, 0),
        "benchmarks": benches,
        "rs_mode": rs_snap.rs_mode,
        "benchmark_status": rs_snap.benchmark_status,
        "status": overall,
    }


# ─────────────────────────────────────────────────────────────────────────────
# COCKPIT RENDER
# ─────────────────────────────────────────────────────────────────────────────
def _regime_color(reg: str) -> str:
    return {"STRONG_BULL": G, "BULL": G, "NEUTRAL": Y, "RANGE": Y,
            "WEAK_BEAR": R, "STRONG_BEAR": R, "VOLATILE": R}.get(reg, "")


def _class_color(c: str) -> str:
    return {"ACTION_NOW": G, "WATCHLIST": Y, "EXTENDED": D, "AVOID": R}.get(c, "")


def _bench_color(status: str) -> str:
    return {"live": G, "OK": G, "cache": Y, "missing": R, "MISSING": R}.get(status, "")


_IST = timezone(timedelta(hours=5, minutes=30))


def _drop_partial_today(df: pd.DataFrame) -> pd.DataFrame:
    """Drop today's still-forming intraday candle so volume math never divides a
    PARTIAL session by complete-day averages (the RVOL≈0.1x bug). The market closes
    15:30 IST; before ~16:00 the last bar dated today is incomplete."""
    try:
        last = df.index[-1]
        last_date = last.date() if hasattr(last, "date") else None
    except Exception:
        last_date = None
    if last_date is not None:
        now = datetime.now(_IST)
        if last_date == now.date() and now.hour < 16:
            return df.iloc[:-1]
    return df


def _volume_context(df: pd.DataFrame) -> dict:
    """Relative volume, pullback from 20D high, volume trend (display overlay — no scoring).
    Always computed on the last COMPLETE session (today's partial candle is dropped)."""
    if df is None or len(df) < 22:
        return {}
    df = _drop_partial_today(df)
    if len(df) < 22:
        return {}
    close, vol = df["Close"], df["Volume"]
    vol20_avg = float(vol.iloc[-21:-1].mean())
    if vol20_avg <= 0:
        return {}
    rvol = round(float(vol.iloc[-1]) / vol20_avg, 2)
    vol3_avg = float(vol.iloc[-4:-1].mean()) if len(vol) >= 4 else float(vol.iloc[-2])
    vol_trend_3d = round(vol3_avg / vol20_avg, 2)
    h20 = float(df["High"].iloc[-20:].max())
    current = float(close.iloc[-1])
    pullback_pct = round((h20 - current) / h20 * 100, 1) if h20 > 0 else 0.0
    ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    return {
        "rvol": rvol,
        "pullback_pct": pullback_pct,
        "vol_trend_3d": vol_trend_3d,
        "above_ema20": current >= ema20 * 0.97,
    }


def _is_extended_pullback(vc: dict) -> bool:
    """True when an EXTENDED stock shows a controlled pullback: 3-12% off 20D high,
    volume drying up (last-3D avg ≤ 0.85× 20D avg), still at/above EMA20 (within 3%)."""
    return (bool(vc)
            and 3.0 <= vc.get("pullback_pct", 0) <= 12.0
            and vc.get("vol_trend_3d", 1.0) <= 0.85
            and vc.get("above_ema20", False))


def _rvol_fmt(vc: dict) -> str:
    """4-visible-char RVOL string for the board. Green = high volume, dim = thin."""
    rvol = vc.get("rvol") if vc else None
    if rvol is None:
        return f"{D} n/a{RST}"
    s = f"{min(rvol, 9.9):.1f}x"   # always 4 visible chars
    if rvol >= 2.0:
        return f"{G}{s}{RST}"
    if rvol < 0.65:
        return f"{D}{s}{RST}"
    return s


def _render_data_quality(dq: dict) -> None:
    """Per-source coverage + an overall HEALTHY/DEGRADED/FAILED verdict, plus a
    loud degraded-mode banner when the benchmark is unavailable. Display only."""
    if not dq:
        return
    benches = dq.get("benchmarks", {})
    overall = dq.get("status", "HEALTHY")
    oc = {"HEALTHY": G, "DEGRADED": Y, "FAILED": R}.get(overall, "")
    cells = "   ".join(f"{name} {_bench_color(st)}{st.upper()}{RST}"
                       for name, st in benches.items())
    print(f"\n  {B}DATA QUALITY{RST}   {oc}{B}{overall}{RST}")
    print(f"  {D}Stocks {dq.get('stocks_pct', 0):.0f}%{RST}   {cells}")
    if dq.get("benchmark_status") != "OK" or dq.get("rs_mode") == "SECTOR_FALLBACK":
        print(f"  {R}{'─'*72}{RST}")
        print(f"  {R}⚠  BENCHMARK DATA UNAVAILABLE — DEGRADED MODE{RST}")
        print(f"  {Y}   RS computed using sector-relative fallback only. "
              f"Regime confidence: LOW.{RST}")
        print(f"  {Y}   Missing data is NOT bearish — treat regime / ACTION_NOW "
              f"with caution.{RST}")
        print(f"  {R}{'─'*72}{RST}")
    elif overall != "HEALTHY":
        print(f"  {Y}   ⚠ Some benchmarks served from cache (stale). "
              f"Regime uses last-known values.{RST}")


# Contexts that represent a tradable entry RIGHT NOW (mirror actionability_engine).
_TRADABLE_CTX = {"PULLBACK_SETUP", "BREAKOUT_SETUP", "TREND_CONTINUATION"}
_GENUINE_MAX_EXT = 5.0      # >5% above EMA20/breakout = chasing, make it WAIT

# The ONLY directionally-proven edge in the 5y validation is RS leadership
# (MARKET_LEADER > SECTOR_LEADER, t=5.01; both beat the field in BULL). A strong
# composite alone is not enough — a high score with NEUTRAL/LAGGARD RS is a pretty
# chart with no proven edge, so require the name to actually BE a leader before
# committing capital. (No score is recomputed; this gates the DECISION only.)
_LEADER_TIERS = {"MARKET_LEADER", "SECTOR_LEADER", "EMERGING_LEADER"}
# Phase-8 validation: names that have held TOP_10 for 8+ consecutive weeks
# MEAN-REVERT (momentum exhaustion). Defer them to WAIT — never buy the top.
_EXHAUSTION_WEEKS = 8
# Never risk more than this per share, even if structure sits deeper (caps blow-ups).
_MAX_STOP_PCT = 12.0


# Breakout confirmation needs real participation. 1.2× is deliberately looser
# than the 1.5× "fake-out" display heuristic: the trigger already requires an RS
# leader at a tradable location — volume is the confirm, not the edge.
_BREAKOUT_RVOL = 1.2


def _entry_confirmation(row, df: "pd.DataFrame | None") -> dict:
    """EXPLICIT price/volume trigger check, computed ONLY on the last COMPLETE
    session (today's partial candle is dropped — same policy as RVOL).

      breakout (BREAKOUT_SETUP):  close above the prior-20d pivot high on
                                  RVOL ≥ 1.2 — a confirmed breakout bar.
      reclaim  (PULLBACK_SETUP /
                TREND_CONTINUATION): an up-day close ABOVE the prior bar's high
                                  — the "1 up-day confirm" reversal bar the
                                  alerts always asked for, now mechanical.

    This is the 2026-07-02 deadlock fix: it converts ARMED leaders into
    SIGNAL_TRIGGERED without any score threshold — no RR, no act ≥ 80.
    Deterministic EOD logic; no intraday noise, no discretion."""
    out = {"breakout": False, "reclaim": False, "why": "no data", "rvol": None,
           "pivot": None}
    if df is None or len(df) < 23:
        return out
    d = _drop_partial_today(df)
    if len(d) < 22:
        return out
    close = float(d["Close"].iloc[-1])
    prev_close = float(d["Close"].iloc[-2])
    prev_high = float(d["High"].iloc[-2])
    vol20 = float(d["Volume"].iloc[-21:-1].mean())
    rvol = (float(d["Volume"].iloc[-1]) / vol20) if vol20 > 0 else 0.0
    out["rvol"] = round(rvol, 2)
    stop = float(getattr(row, "stop", 0) or 0)
    if stop and close <= stop:                       # already through the stop
        out["why"] = f"last close {_fmt_money(close)} at/below stop — setup broken"
        return out

    ctx = getattr(row, "entry_context", "")
    if ctx == "BREAKOUT_SETUP":
        pivot = float(d["High"].iloc[-21:-1].max())  # 20d pivot BEFORE the last bar
        out["pivot"] = pivot
        if close > pivot and rvol >= _BREAKOUT_RVOL:
            out["breakout"] = True
            out["why"] = (f"confirmed: closed {_fmt_money(close)} above pivot "
                          f"{_fmt_money(pivot)} on {rvol:.1f}x volume")
        else:
            need = (f"close > {_fmt_money(pivot)}"
                    if close <= pivot else f"volume ≥ {_BREAKOUT_RVOL:.1f}x")
            out["why"] = f"awaiting breakout bar ({need}; last {rvol:.1f}x vol)"
    elif ctx in ("PULLBACK_SETUP", "TREND_CONTINUATION"):
        if close > prev_close and close > prev_high:
            out["reclaim"] = True
            out["why"] = (f"confirmed: up-day close {_fmt_money(close)} above prior "
                          f"high {_fmt_money(prev_high)} — pullback reclaimed")
        else:
            out["why"] = (f"awaiting reversal bar (up-day close > prior high "
                          f"{_fmt_money(prev_high)})")
    else:
        out["why"] = f"{ctx.lower() or 'no setup'} — no trigger defined"
    return out


def _is_genuine_entry(row, leader_weeks: "dict | None" = None) -> bool:
    """A real entry to take TODAY: a PROVEN LEADER at a tradable location — not
    extended/chasing, not a mature (exhaustion-risk) leader, RR never a gate.

    SINGLE SOURCE OF TRUTH: this delegates entirely to lifecycle_states.
    evaluate_trigger's setup gates (the exact same five checks used to arm and
    trigger names) — the previous copy of these gates here had already started
    to drift, which is how split-brain contradictions are born. `armed` in the
    TriggerDecision means "passes every setup gate" (whether or not a
    confirmation bar has printed), which is precisely "genuine entry"."""
    if row is None:
        return False
    d = _lc_decide(row, leader_weeks, None)
    return d.armed


def _is_signal_triggered(row, leader_weeks: "dict | None" = None,
                         confirm: "dict | None" = None) -> bool:
    """The execution-queue gate (state machine: WATCHLIST → SIGNAL_TRIGGERED).

    A name is "entry-ready NOW" — i.e. it may emit a buy order TODAY — only when
    it passes every setup gate AND an EXPLICIT trigger has fired: ACTION_NOW,
    a confirmed breakout bar, or a confirmed pullback-reclaim bar (both computed
    on the last COMPLETE session by _entry_confirmation). A pretty pullback with
    no confirmation bar is ARMED, not triggered — it gets an exact CONDITIONAL
    order instead of a market buy. ONE evaluation: portfolio/lifecycle_states.
    """
    return _lc_triggered(row, leader_weeks, confirm)


def _is_armed(row, leader_weeks: "dict | None" = None,
              confirm: "dict | None" = None) -> bool:
    """Genuine leader setup that is WAITING for a trigger. Armed = passes every
    setup gate but the confirmation bar hasn't printed yet → gets a fully-sized
    CONDITIONAL order (GTT trigger/stop/targets/qty), never a chase."""
    return _lc_armed(row, leader_weeks, confirm)


def _armed_reason(row, confirm: "dict | None" = None) -> str:
    """Accurate 'why not yet' line for an ARMED (untriggered) leader setup."""
    why = (confirm or {}).get("why") or "no confirmation bar yet"
    return (f"armed — proven leader at a {row.entry_context.replace('_', ' ').lower()} · "
            f"{why}")


def _regime_posture(regime: str) -> "tuple[str, str]":
    """E4 (validated risk overlay): the MARKET_LEADER edge holds only in BULL and
    REVERSES in BEAR (leaders lose to laggards, t≈−3.3 OOS); the BULL-only gate
    roughly halves max drawdown. Gate NEW long entries by regime.

    This is a risk overlay on the DECISION — it changes no score, no regime label,
    no ranking. It only decides whether to commit FRESH capital:
        DEPLOY       — BULL family: take genuine leader entries.
        CAUTION      — NEUTRAL/RANGE: edge weak/unproven — half-size, top names only.
        STAND_ASIDE  — BEAR/VOLATILE: negative-expectancy for leaders — no new longs."""
    r = (regime or "").upper()
    if "BULL" in r:
        return "DEPLOY", ""
    if "BEAR" in r or "VOLATILE" in r:
        return ("STAND_ASIDE",
                f"{regime}: leaders historically REVERSE in this regime (lose to "
                f"laggards, t≈−3.3 OOS). Fresh leader longs are negative-expectancy "
                f"here — preserve capital and let the tape turn.")
    return ("CAUTION",
            f"{regime}: the leader edge is weak/unproven outside BULL (~+0.5%, not "
            f"significant). Half-size, highest-conviction names only, tighten alerts.")


def _entry_timing(row) -> str:
    """Plain-English trigger for a genuine BUY-TODAY name."""
    ctx = row.entry_context if row else "NO_SETUP"
    return {
        "PULLBACK_SETUP":     "limit buy at entry · stop is below support · valid 3 trading days",
        "BREAKOUT_SETUP":     "buy only if today's volume ≥ 1.5× 20D avg (else it's a fake-out)",
        "TREND_CONTINUATION": "limit buy in the zone · skip if it gaps >2% above the zone",
    }.get(ctx, "limit buy in the entry zone")


def _wait_trigger(row) -> str:
    """For a strong name that is NOT a clean entry now: what to wait for + alert price.
    Prevents buying highs / catching knives — you act only when price comes to you."""
    if row is None:
        return "monitor"
    ext = row.extension_pct
    if row.classification == "EXTENDED" or ext > _GENUINE_MAX_EXT:
        # approximate the EMA20/breakout pullback level it would re-enter at
        level = row.entry / (1 + max(ext, 0.1) / 100.0)
        return (f"extended {ext:.0f}% above support — WAIT for pullback · "
                f"set alert ≈ {_fmt_money(level)} (near EMA20) · confirm with 1 up-day")
    if row.entry_context == "BASE_BUILDING":
        return (f"still building base — WAIT for breakout · "
                f"set alert ≈ {_fmt_money(row.entry)} (zone top)")
    return f"no clean setup — monitor · alert ≈ {_fmt_money(row.entry)}"


def _defer_reason(row, leader_weeks: "dict | None" = None) -> str:
    """Why a strong board name is deferred to WAIT — leadership/exhaustion-aware so
    the trader sees the REASON (not a leader yet / mature leader), not just a price."""
    if row is None:
        return "monitor"
    wks = (leader_weeks or {}).get(row.ticker, 0)
    if wks >= _EXHAUSTION_WEEKS:
        return (f"mature leader ({wks} wks in TOP_10) — mean-reversion risk · wait for "
                f"a reset/re-accumulation, alert ≈ {_fmt_money(row.entry)}")
    if row.rs_status not in _LEADER_TIERS:
        return (f"not an RS leader yet ({row.rs_status.replace('_', ' ').lower()}) — "
                f"strong setup, unproven edge · monitor for a leadership upgrade")
    return _wait_trigger(row)


def _fmt_money(p) -> str:
    if p is None:
        return "—"
    return f"₹{p:,.0f}" if p >= 100 else f"₹{p:,.2f}"


def _atr_pct_for(ticker, feed) -> "float | None":
    """Current ATR% for a candidate — the cohort axis for path-analog lookup."""
    df = feed.get(ticker) if feed else None
    if df is None or len(df) < 15:
        return None
    try:
        from scanner.scanner import compute_atr_series
        atr = float(compute_atr_series(df).iloc[-1])
        close = float(df["Close"].iloc[-1])
        return atr / close * 100 if close else None
    except Exception:
        return None


def _atr_for(ticker, feed) -> "float | None":
    """Current ATR in ₹ for a candidate — drives the calibrated stop/target levels."""
    df = feed.get(ticker) if feed else None
    if df is None or len(df) < 15:
        return None
    try:
        from scanner.scanner import compute_atr_series
        return float(compute_atr_series(df).iloc[-1])
    except Exception:
        return None


def _load_calibrated():
    """Calibrated ATR-multiple stop/T1/T2 levels (evidence-based, OOS-validated)."""
    import json
    p = Path("reports/validation/calibrated_targets.json")
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _earnings_days(ticker: str) -> "int | None":
    """Days until the next earnings date (None if unknown). Used by the trade-quality
    death-zone check. Reuses the existing cached earnings calendar; fails silently."""
    try:
        from scanner.earnings_calendar import next_earnings
        iso = next_earnings(ticker)
        if not iso:
            return None
        return (datetime.fromisoformat(iso).date() - date.today()).days
    except Exception:
        return None


def _path_evidence(cache, regime, rs_status, atr_pct) -> str:
    """Real historical-analog behaviour for this candidate's cohort: probability of
    reaching +10/+15/+20% BEFORE the stop, EV, median hold, typical drawdown.
    Descriptive only — the EV ranking FAILED its OOS gate (corr ≈ −0.14), so this
    informs expectations, it does NOT pick or rank the trade."""
    if not cache:
        return ""
    try:
        from analytics.path_intelligence import lookup_cohort
    except ImportError:
        return ""
    s = lookup_cohort(cache, regime, rs_status, atr_pct)
    if not s:
        return ""
    approx = "~" if s.get("approx") else ""
    return (f"analogs{approx} n={int(s['n'])}: reach +10% {s.get('p10')}% · "
            f"+15% {s.get('p15')}% · +20% {s.get('p20')}% before stop · "
            f"EV {s['ev_pct']:+.1f}% · hold {int(s['median_hold'])}d · "
            f"typ. drawdown {s.get('med_mae')}%")


def _evidence_note(stats, rs_status, classification) -> str:
    """One-line 5y reference for a candidate's cohort — what history actually did."""
    try:
        from analytics.setup_stats import expected_move
    except ImportError:
        return ""
    e = expected_move(stats, rs_status, classification)
    if not e:
        return ""
    mae = e.get("med_mae20_pct")
    mae_s = f" · typ. drawdown {mae:.0f}%" if mae is not None else ""
    return (f"5y: {e['win20_pct']:.0f}% win · median +{e['med20_pct']:.1f}% / "
            f"p75 +{e['p75_20_pct']:.1f}% (20d){mae_s} · +{e['med60_pct']:.1f}% by 60d")


def _target_reality(stats, rs_status, classification, entry, t1) -> str:
    """Reality-check a geometric T1 against the historical 20d move distribution —
    so the trader sees whether the target is realistic or a right-tail dream."""
    try:
        from analytics.setup_stats import expected_move
    except ImportError:
        return ""
    e = expected_move(stats, rs_status, classification)
    if not e or not (entry and t1 and t1 > entry):
        return ""
    move = (t1 - entry) / entry * 100
    p90 = e.get("p90_20_pct", e["p75_20_pct"])
    if move >= p90:
        tag = f"{R}top-10% outcome — ~1 in 10 reach it in 20d{RST}"
    elif move >= e["p75_20_pct"]:
        tag = f"{Y}top-25% outcome — ~1 in 4 reach it in 20d{RST}"
    elif move >= e["med20_pct"]:
        tag = f"{G}above median — realistic for a winner{RST}"
    else:
        tag = "within typical 20d range"
    return (f"T1 +{move:.0f}% vs hist median +{e['med20_pct']:.1f}% / "
            f"p75 +{e['p75_20_pct']:.1f}% → {tag}")


def _render_account_block(state, fresh: bool, live: bool) -> None:
    """ALWAYS-ON account panel from REAL broker state — never config/phantom capital."""
    src = state.source or "UNKNOWN"
    n = len(state.holdings)
    if live:
        tag = f"{G}{B}LIVE{RST}"
    elif "ZERODHA_LIVE" in src and not fresh:
        tag = f"{R}{B}STALE SESSION{RST}"
    elif state.net_equity <= 0:
        tag = f"{R}{B}EMPTY{RST}"
    else:
        tag = f"{Y}{src}{RST}"
    asof = (state.as_of or "")[:16].replace("T", " ")
    print(f"\n  {B}ACCOUNT{RST}  {tag}   {D}as of {asof}{RST}")
    print(f"  {D}  Equity {_fmt_money(state.net_equity)}  ·  Cash "
          f"{_fmt_money(state.available_cash)}  ·  Holdings "
          f"{_fmt_money(state.holdings_value)} ({n} pos)  ·  "
          f"Deployed {state.deployed_pct:.0f}%{RST}")


def _render_watch_only(res: ScreenResult, stats) -> None:
    """No live capital → no sizing. Show what to set ALERTS on (still useful),
    derived straight from the screen, with evidence — but zero phantom quantities."""
    rows = res.act_snap.top_n(20)
    lw = res.leader_weeks
    conf = {r.ticker: _entry_confirmation(r, res.feed.data.get(r.ticker)) for r in rows}
    # State-machine consistent: "entry-ready" REQUIRES a fired trigger (ACTION_NOW
    # or a confirmed breakout/reclaim bar). Armed leaders are alerts only.
    triggered = [r for r in rows if _is_signal_triggered(r, lw, conf.get(r.ticker))]
    armed = [r for r in rows if _is_armed(r, lw, conf.get(r.ticker))]
    waits = [r for r in rows if r.classification in ("WATCHLIST", "EXTENDED")
             and not _is_genuine_entry(r, lw)]
    print(f"\n  {B}WATCH — set price alerts{RST}  {D}(no capital to size — fund to trade){RST}")
    if triggered:
        print(f"  {G}  Entry-ready now — trigger fired (would buy once funded):{RST}")
        for r in triggered[:6]:
            print(f"  {G}  {r.symbol:<12s}{RST}  {D}entry {r.entry_zone}  stop {r.stop_zone}"
                  f"  · {_entry_timing(r)}{RST}")
            ev = _evidence_note(stats, r.rs_status, r.classification)
            if ev:
                print(f"  {D}       {ev}{RST}")
    if armed:
        print(f"  {Y}  Armed — proven setups WAITING for a trigger (alert only, don't buy):{RST}")
        for r in armed[:6]:
            print(f"  {Y}  {r.symbol:<12s}{RST}  {D}{_armed_reason(r, conf.get(r.ticker))}{RST}")
    for r in waits[:6]:
        print(f"  {Y}  {r.symbol:<12s}{RST}  {D}{_defer_reason(r, lw)}{RST}")


def _render_live_journal(js) -> None:
    """Your real executions — the validation that actually counts (feeds the kill switch)."""
    if not js:
        return
    n = js.get("n_closed", 0)
    print(f"\n  {B}LIVE TRADE JOURNAL{RST}  {D}(your real trades — the validation that counts){RST}")
    if not n:
        no = js.get("n_open", 0)
        print(f"  {D}  No closed trades yet ({no} open). Stats build as you trade · "
              f"full dashboard: python run.py --trades{RST}")
        return
    exp = js.get("expectancy_R"); r20 = js.get("rolling20_expectancy_R")
    ec = G if (exp or 0) > 0 else R
    print(f"  {D}  {n} closed · win {js.get('win_rate')}% · expectancy {ec}{exp:+.2f}R{RST}"
          f"{D} · rolling-20 {r20:+.2f}R · PF {js.get('profit_factor')} · "
          f"maxDD {js.get('max_dd_pct')}% · avg hold {js.get('avg_hold')}d{RST}")


def _render_edge_health(h) -> None:
    """Walk-forward: is the proven leader edge alive, dying, or dead? Year-by-year
    excess + recent rolling + the regime-shift reason it decayed."""
    if not h or not h.get("years"):
        return
    yrs = h["years"]
    cells = "  ".join(f"{y} {yrs[y]['excess']:+.1f}%" for y in sorted(yrs))
    re = h.get("recent_excess")
    re_s = f"{re:+.1f}%" if re is not None else "n/a"
    dr = h.get("drivers", {})
    bs = (f"BULL share {dr.get('bull_share_first','?')}→{dr.get('bull_share_last','?')}"
          if dr else "")
    col = G if "ALIVE" in h["verdict"] else (R if "DYING" in h["verdict"] else Y)
    print(f"\n  {B}EDGE HEALTH{RST}  {D}(MARKET_LEADER 20d excess vs universe, by year — "
          f"edges decay, so watch this){RST}")
    print(f"  {D}  {cells}{RST}")
    print(f"  {D}  recent 8wk excess {re_s}  ·  why it decayed: {bs} (regime shift)  ·  "
          f"{h['positive_years']}/{h['total_years']} years positive{RST}")
    print(f"  {col}  Verdict: {h['verdict']}{RST}")


def _grade_color(grade: str) -> str:
    return {"A": G, "B": G, "C": Y, "ELITE": G, "GOOD": G,
            "AVERAGE": Y, "POOR": R, "REJECT": R}.get(grade, "")


def _tier_color(tier: str) -> str:
    return {"PRIME": G, "CAUTION": Y, "HIGH_RISK": R}.get(tier, "")


def _render_trade_quality(tq) -> None:
    """Compact per-trade risk-manager readout (Phase O). Shows the quality grade, the
    risk tier, the six axes, the EXPLICIT risk flags, and the management plan — so the
    trader can see and weigh the risk (nothing is hidden; lower quality = smaller size)."""
    if tq is None:
        return
    gc = _grade_color(tq.grade)
    sc = _grade_color(tq.structure.label)
    tc = _tier_color(tq.risk_tier)
    print(f"  {D}       ↳ {gc}QUALITY {tq.grade} ({tq.final_score:.0f}){RST}{D} "
          f"{tc}{tq.risk_tier}{RST}{D}  ·  structure {sc}{tq.structure.label} "
          f"{tq.structure.score:.0f}{RST}{D}  ·  entry {tq.entry.label.replace('_ENTRY','')} "
          f"{tq.entry.score:.0f}  ·  stop {tq.stop_q.label.replace('_STOP','')} "
          f"{tq.stop_q.score:.0f}{RST}")
    if tq.risk_tier != "PRIME" and tq.headline:
        print(f"  {D}       ↳ {tc}risk: {'; '.join(tq.headline[:3])}{RST}")
    dz, lt = tq.death_zone, tq.liquidity_trap
    dzc = G if dz.level == "LOW" else (Y if dz.level == "MEDIUM" else R)
    ltc = G if lt.level == "LOW" else (Y if lt.level == "MEDIUM" else R)
    flags = f" — {dz.factors[0]}" if dz.factors else ""
    print(f"  {D}       ↳ death-zone {dzc}{dz.level}{RST}{D}  ·  liquidity-trap "
          f"{ltc}{lt.level}{RST}{D}{flags}{RST}")
    print(f"  {D}       ↳ manage: invalidate {_fmt_money(tq.plan.invalidation)} · "
          f"scale ½ @T1 → breakeven · trail {tq.plan.trail_method} · "
          f"exit on close below EMA20{RST}")


def _buy_table_header() -> None:
    print(f"\n  {D}  {'#':>2}  {'STOCK':12s} {'T':>1}  {'QTY':>5}  {'ENTRY':>8}  "
          f"{'STOP':>13}  {'T1':>13}  {'T2':>13}  {'RISK':>7}{RST}")
    print(f"  {D}  {'─'*104}{RST}")


def _render_buy_line(i: int, c: dict, cap: float, risk_pct: float, color: str = G) -> None:
    """One BUY-TODAY row + its detail/quality sub-lines. Reused by the PRIME and the
    lower-quality ('also on the table') sections — same info, different colour.
    `risk_pct` must be the EFFECTIVE (adaptive) risk %/trade — the same number
    that sized the position — so the R-allocation shown is the R actually taken."""
    entry = c["entry"]

    def _lvl(p):
        return (f"₹{p:>6,.0f} ({(p/entry-1)*100:>+4.1f}%)" if p else "      —       ")

    r_alloc = c["risk"] / (cap * risk_pct / 100.0) if (cap and risk_pct) else 0.0
    print(f"  {color}  {i:>2}  {c['sym'][:12]:12s} {c['tier']:>1}  {c['qty']:>5}  "
          f"₹{entry:>6,.0f}  {_lvl(c['stop'])}  {_lvl(c['t1'])}  {_lvl(c['t2'])}  "
          f"₹{c['risk']:>5,.0f}{RST}")
    lv = c["lv"]
    pos_val = c["qty"] * entry
    if lv:
        print(f"  {D}       ↳ T1 {lv['p_t1']*100:.0f}% hit ~{lv['days_t1']}d · "
              f"T2 {lv['p_t2']*100:.0f}% hit ~{lv['days_t2']}d · alloc {r_alloc:.1f}R "
              f"(tier {c['tier']}, conv {c['conv']:.0f}) · bank ½ at T1, trail rest{RST}")
        hold_s = (f"expected hold ~{lv['days_t1']}–{lv['days_t2']}d · "
                  f"hard time-stop: exit day 20 if T1 not hit")
    else:
        hold_s = "expected hold 20–60d (5y cohort) · hard time-stop: exit day 20 if T1 not hit"
    print(f"  {D}       ↳ position {_fmt_money(pos_val)} "
          f"({pos_val/cap*100:.1f}% of equity) · {hold_s}{RST}")
    print(f"  {D}       ↳ {_entry_timing(c['row'])}{RST}")
    _render_trade_quality(c.get("tq"))
    if c["warn"]:
        print(f"  {Y}       ⚠ {c['warn']}{RST}")
    try:
        from scanner.earnings_calendar import earnings_warning
        ew = earnings_warning(f"{c['sym']}.NS")
        if ew:
            print(f"  {R}       {ew}{RST}")
    except Exception:
        pass


def _render_evidence_footer(stats) -> None:
    """Compact 5y reality anchor + honest scope + reliability caveat."""
    if stats:
        ml = stats.get("by_rs", {}).get("MARKET_LEADER", {})
        print(f"\n  {B}SETUP EVIDENCE{RST}  {D}(5y · {stats['n_obs']:,} obs · "
              f"{stats['date_min']}→{stats['date_max']}){RST}")
        if ml:
            print(f"  {D}  Market leaders: {ml['win20_pct']:.0f}% win · median "
                  f"+{ml['med20_pct']:.1f}% / p75 +{ml['p75_20_pct']:.1f}% (20d) · typ. "
                  f"drawdown {ml.get('med_mae20_pct', 0):.0f}% · hold 20–60d "
                  f"(+{ml['med60_pct']:.1f}% by 60d){RST}")
        print(f"  {G}  ✓ Targets are ATR-CALIBRATED to the real OOS fill distribution "
              f"(T1≈59% hit ~8d, T2≈42% hit ~17d) — not geometric outliers.{RST}")
        print(f"  {D}  Stops sit beyond typical winner heat (3×ATR; ~85% of winning analogs "
              f"never hit them). Size for the real −7% to −12% drawdown.{RST}")
        print(f"  {D}  ⓘ RR & analog-EV are NON-predictive OOS — shown as context only, never "
              f"used to rank, filter or size.{RST}")
    print(f"  {D}  ⓘ Screen rated 'Research Only' (Trust 53/100; edge failed 2025 OOS). "
          f"Size small, honour every stop.{RST}")


def _render_exit_rules(calib) -> None:
    """The COMPLETE mechanical exit policy — one ruleset, first condition wins,
    zero discretion left. Every number is measured, never invented: T1/T2 fill
    odds and median times from target_calibration (OOS-validated), the
    20-session time stop from the same calibration window (winners reach T1 in
    ~8d median), the regime exit from the E4 validation (leader edge reverses
    in BEAR, t≈−3.3 OOS)."""
    p1 = f"{calib.get('p_t1_test', 0) * 100:.0f}%" if calib else "~59%"
    p2 = f"{calib.get('p_t2_test', 0) * 100:.0f}%" if calib else "~42%"
    d1 = calib.get("days_t1", 8) if calib else 8
    d2 = calib.get("days_t2", 17) if calib else 17
    print(f"\n  {B}  MANAGE — MECHANICAL EXIT RULES{RST}  "
          f"{D}(first condition hit wins · no discretionary exits){RST}")
    print(f"  {D}   on fill          → run `python run.py --positions` — auto-places the "
          f"protective SL GTT at the stop{RST}")
    print(f"  {D}   T1 hit ({p1} ~{d1}d)  → sell ½, stop → breakeven:  "
          f"`python run.py --partial TICKER QTY`{RST}")
    print(f"  {D}   T2 hit ({p2} ~{d2}d) → exit the rest — or trail 2×ATR only while "
          f"regime is BULL and the name is still an RS leader{RST}")
    print(f"  {D}   day 20, no T1    → exit next open — calibrated winners move by "
          f"~{d1}d; stale capital is dead capital{RST}")
    print(f"  {D}   regime flips BEAR/VOLATILE at the weekly screen → exit anything "
          f"below T1 (E4: leader edge reverses, t≈−3.3 OOS){RST}")
    print(f"  {D}   gap through stop → exit at the open — never widen a stop{RST}")


def _render_cash_proof(res: ScreenResult, edge_h) -> None:
    """STAY IN CASH must be EARNED (wait-policy): print the checks with MEASURED
    values so cash is a proven position, never a lazy default. If any check had
    failed, the plan above would contain an order instead."""
    reg = res.regime
    hostile = not ("BULL" in (reg.regime or "").upper())
    top = res.act_snap.top_n(50)
    n_leaders = sum(1 for r in top if r.rs_status in _LEADER_TIERS)
    n_setup = sum(1 for r in top if r.rs_status in _LEADER_TIERS
                  and r.entry_context in _TRADABLE_CTX)
    re_ = (edge_h or {}).get("recent_excess")
    re_s = f"{re_:+.1f}%" if re_ is not None else "unknown (dataset stale)"
    print(f"\n  {B}  WHY CASH WINS TODAY{RST}  "
          f"{D}(wait-policy checks — measured, not vibes){RST}")
    print(f"  {D}   1. regime hostile?  "
          f"{'YES — ' + reg.regime + ' (E4: no fresh leader longs)' if hostile else 'NO — ' + reg.regime + f' {reg.regime_score:.0f}/100 — regime is not the blocker'}{RST}")
    print(f"  {D}   2. tradable candidates?  {n_leaders} RS leaders on the board, "
          f"{n_setup} at a tradable location, 0 passed the entry gates "
          f"(extension/exhaustion/structure) — the proven cohort is "
          f"leader + location, and location is absent{RST}")
    print(f"  {D}   3. recent edge health: {re_s} 8-week leader excess — "
          f"the edge state is not the constraint, setup supply is{RST}")
    print(f"  {D}   4. forcing an unconfirmed entry ≈ universe expectancy "
          f"(+1.96% 20d, −5.4% median heat) minus costs — worse risk-adjusted "
          f"than waiting one session for a trigger{RST}")
    print(f"  {D}   5. therefore: cash. Re-run tomorrow — triggers and conditional "
          f"orders appear mechanically when a confirmation bar prints.{RST}")


def _render_zero_decision_plan(res: ScreenResult) -> None:
    """
    Zero-decision trade plan appended to --screen. REAL-broker capital only:
      • sizes against real Zerodha equity — fresh session, or last-known equity
        loudly flagged STALE (never config/phantom capital);
      • BUY TODAY only for genuine entries with a fired trigger (RS leader at a
        tradable location + confirmation bar; RR is never a gate — disproven);
      • armed leaders become exact CONDITIONAL GTT orders, not vague alerts;
      • every target is reality-checked against 5y forward-return evidence;
      • ends with ONE mechanical exit ruleset — nothing left to decide.

    No scores changed. Pure display, consumes the already-built ScreenResult.
    """
    try:
        from portfolio.portfolio_engine import PortfolioConstructor
        from portfolio.lifecycle_engine import LifecycleManager
        from portfolio.portfolio_state import (load_state, load_deployment_config,
                                               state_to_lifecycle_holdings)
        from deploy.order_card import build_order_card
        from analytics.setup_stats import load_setup_stats
        from analytics.path_intelligence import load_cohort_cache
    except ImportError as exc:
        print(f"\n  {Y}  [trade plan unavailable: {exc}]{RST}")
        return

    cfg        = load_deployment_config()
    cfg_cap    = float(cfg.get("capital", 500_000))   # used ONLY by load_state's NAV compare
    risk_pct   = float(cfg.get("risk_per_trade_pct", 1.0))
    time_stop  = int(cfg.get("time_stop_weeks", 12))
    # ONE position-cap truth (config/deployment.yaml) — was three disagreeing
    # hardcoded numbers (plan 3, yaml 5, constructor 8). Committee rule:
    # never hold more than target_positions total, never open more than
    # max_new_per_week fresh names in one review.
    target_pos = int(cfg.get("target_positions", 5))
    max_new    = int(cfg.get("max_new_per_week", 3))
    stats      = load_setup_stats()
    path_cache = load_cohort_cache()
    calib      = _load_calibrated()
    try:
        from analytics.edge_health import load as _load_edge_health
        edge_h = _load_edge_health()
    except Exception:
        edge_h = None
    try:
        from analytics.adaptive_risk import risk_mode as _risk_mode, kill_switch as _kill_switch
        from analytics.trade_journal import load_stats as _journal_stats, snapshot_plan as _snapshot_plan
        journal_stats = _journal_stats()
    except Exception:
        _risk_mode = _kill_switch = _snapshot_plan = None
        journal_stats = {"n_closed": 0, "n_open": 0}
    # Trade Quality Intelligence Layer (Phase O) — optional capital-preservation gate.
    try:
        from analytics.trade_quality import assess_trade as _assess_trade
        from analytics.path_intelligence import lookup_cohort as _lookup_cohort
        from analytics.setup_stats import expected_move as _expected_move
    except Exception:
        _assess_trade = _lookup_cohort = _expected_move = None

    try:
        state = load_state(cfg_cap)
    except Exception as exc:
        print(f"\n  {Y}  [account state error: {exc}]{RST}")
        return

    today = date.today().isoformat()
    fresh = (state.as_of or "")[:10] == today
    live  = ("ZERODHA_LIVE" in (state.source or "")) and state.net_equity > 0 and fresh

    print(f"\n  {B}{'═'*72}{RST}")
    print(f"  {G}{B}  ZERO-DECISION TRADE PLAN{RST}")
    print(f"  {B}{'═'*72}{RST}")
    _render_account_block(state, fresh, live)

    # ── NOT LIVE → two honest cases. Phantom/config capital is still banned:
    #    • real-but-STALE broker equity → produce the FULL plan sized on the
    #      last-known equity, loudly flagged (a dated real number beats "no
    #      plan" — zero-decision output needs quantities), placement blocked
    #      behind --kite-login anyway;
    #    • empty/unknown account → watch-only, no sizing, as before.
    if not live:
        stale_known = ("ZERODHA_LIVE" in (state.source or "")) and state.net_equity > 0
        if stale_known:
            print(f"  {R}  ⚠ Kite session expired (last live sync "
                  f"{(state.as_of or '?')[:10]}) — sizing uses LAST-KNOWN equity "
                  f"{_fmt_money(state.net_equity)}.{RST}")
            print(f"  {R}    Run `python run.py --kite-login` BEFORE placing any "
                  f"order below (quantities re-check on live cash).{RST}")
        else:
            if state.net_equity <= 0:
                print(f"  {R}  ⚠ ₹0 deployable — fund your Zerodha account. "
                      f"No buy plan until capital is live.{RST}")
            _render_watch_only(res, stats)
            _render_evidence_footer(stats)
            return

    # ── LIVE → size against REAL equity, ADAPTIVE risk by edge health ───────
    cap = state.net_equity
    rm = (_risk_mode(edge_h, res.regime.regime, risk_pct) if _risk_mode
          else {"mode": "—", "risk_pct": risk_pct, "max_heat_pct": 6.0, "reason": ""})
    ks = (_kill_switch(edge_h, journal_stats) if _kill_switch
          else {"paused": False, "reasons": [], "recovery": []})
    eff_risk, eff_heat = rm["risk_pct"], rm["max_heat_pct"]
    mode_col = G if rm["mode"] == "STRONG" else (Y if rm["mode"] == "MIXED" else R)
    print(f"  {B}  RISK MODE: {mode_col}{rm['mode']}{RST}{D}  ·  risk {eff_risk:.2f}%/trade "
          f"(≈ {_fmt_money(cap * eff_risk / 100)})  ·  max book heat {eff_heat:.1f}%{RST}")
    print(f"  {D}  {rm['reason']}{RST}")

    # ── KILL SWITCH → survival first: no new entries in a hostile environment ─
    if ks["paused"]:
        print(f"\n  {R}{B}  ② NO TRADE TODAY — STRATEGY PAUSED{RST}")
        for rsn in ks["reasons"]:
            print(f"  {R}  ✗ {rsn}{RST}")
        print(f"  {Y}  Reactivate only when: {' · '.join(ks['recovery'])}{RST}")
        _render_live_journal(journal_stats)
        _render_edge_health(edge_h)
        _render_evidence_footer(stats)
        return

    try:
        snap    = PortfolioConstructor(risk_per_trade=risk_pct,
                                       max_positions=target_pos).construct(
                      res.act_snap, res.regime, res.feed.data, persist=False)
        lc      = state_to_lifecycle_holdings(state)
        lc_snap = LifecycleManager(time_stop_weeks=time_stop).review(lc, res, persist=False)
        card    = build_order_card(snap, state, cap, lc_snap=lc_snap, posture="full")
    except Exception as exc:
        print(f"\n  {Y}  [order card error: {exc}]{RST}")
        return

    # ① EXIT first (free cash before opening new positions) — runs in EVERY regime,
    #    risk management on existing holdings is never gated off.
    if card.sells:
        print(f"\n  {R}{B}  ① EXIT NOW{RST}")
        for s in card.sells:
            pnl_s = f"  P&L {s.pnl_pct:+.1f}%" if s.pnl_pct else ""
            print(f"  {R}  SELL  {s.symbol:<12s}  {s.quantity} sh{pnl_s}  → {s.reason}{RST}")

    # ── REGIME GATE (E4, validated): the leader edge holds only in BULL and REVERSES
    #    in BEAR (t≈−3.3 OOS). Stand aside on NEW longs in BEAR/VOLATILE — fresh
    #    leader entries are negative-expectancy there. Risk overlay only; no score,
    #    regime label or ranking is changed. EXIT/HOLD management above still runs.
    posture, posture_msg = _regime_posture(res.regime.regime)
    if posture == "STAND_ASIDE":
        print(f"\n  {R}{B}  ② NO NEW LONGS — {res.regime.regime} REGIME{RST}")
        print(f"  {R}  ✗ {posture_msg}{RST}")
        print(f"  {Y}  Manage existing positions (stops/exits above); build a ready list "
              f"below; re-deploy only when the regime turns BULL.{RST}")
        watch = [(r.symbol, _defer_reason(r, res.leader_weeks))
                 for r in res.act_snap.top_n(20) if r.classification != "AVOID"]
        if watch:
            print(f"\n  {Y}{B}  ③ WATCH — SET PRICE ALERTS{RST}  "
                  f"{D}(ready list for when the tape turns — do not buy yet){RST}")
            for sym, msg in watch[:10]:
                print(f"  {Y}  {sym:<12s}{RST}  {D}{msg}{RST}")
        if card.holds:
            print(f"\n  {D}  HOLD: {' · '.join(h.symbol for h in card.holds[:8])}{RST}")
        _render_live_journal(journal_stats)
        _render_edge_health(edge_h)
        _render_evidence_footer(stats)
        return
    if posture == "CAUTION":
        print(f"\n  {Y}  ⚠ {posture_msg}{RST}")

    # ── EXPLICIT TRIGGERS (2026-07-02 fix): compute the price/volume confirmation
    #    for every buy candidate on the last COMPLETE session, then split into
    #    TRIGGERED-entry-now vs armed/wait. STATE-MACHINE GATE: only a name with an
    #    explicit trigger (ACTION_NOW / confirmed breakout bar / confirmed reclaim
    #    bar) may enter the execution queue. Armed-but-untriggered leaders get an
    #    exact CONDITIONAL order (GTT) below — placed, never chased.
    confirm: dict[str, dict] = {}
    for b in card.buys:
        row = res.act_snap.get(b.ticker)
        if row is not None:
            confirm[b.ticker] = _entry_confirmation(row, res.feed.data.get(b.ticker))
    buy_now, wait = [], []
    for b in card.buys:
        row = res.act_snap.get(b.ticker)
        (buy_now if _is_signal_triggered(row, res.leader_weeks, confirm.get(b.ticker))
         else wait).append((b, row))

    # ② BUY TODAY — calibrated levels + portfolio-risk caps + conviction-tier sizing
    use_calib = bool(calib and calib.get("k_stop"))
    if use_calib:
        from analytics.target_calibration import apply_levels
        note = (f"stop {calib['k_stop']}R · T1 {calib['k_t1']}R ({calib.get('p_t1_test',0)*100:.0f}% hit OOS) · "
                f"T2 {calib['k_t2']}R ({calib.get('p_t2_test',0)*100:.0f}% hit OOS)")
    else:
        note = "RR ignored — non-predictive"

    conv_map      = {p.symbol: p for p in snap.positions}
    ATR_CEIL_PCT  = 4.0      # beyond this daily ATR a name is too jumpy for clean levels
    SECTOR_CAP    = 2        # max names per sector — kills correlated-cluster blow-ups
    MAX_BOOK_HEAT = eff_heat # adaptive: scaled by risk mode (edge health)

    def _tier(conv):
        return "A" if conv >= 85 else ("B" if conv >= 70 else "C")

    # sector grade map (for the death-zone "sector weakening" check)
    sector_grade_map = {r.sector: r.grade for r in res.sector_snap.rows}

    # ── sized-candidate builder — shared by CONFIRMED buys (entry = today's
    #    limit) and ARMED conditionals (entry = the GTT trigger price). Same
    #    calibrated levels, structure-aware stop, quality sizing for both, so a
    #    conditional order is exactly the order a fill would deserve.
    def _sized_candidate(b, row, entry_override: "float | None" = None):
        entry = entry_override or b.limit_price
        atr_raw = _atr_for(b.ticker, res.feed.data)
        atr_pct = (atr_raw / entry * 100) if (atr_raw and entry) else None
        warn, atr = "", atr_raw
        if atr_raw and atr_pct and atr_pct > ATR_CEIL_PCT:
            atr = entry * ATR_CEIL_PCT / 100.0
            warn = f"high ATR {atr_pct:.1f}% — levels volatility-capped (jumpy name, size kept small)"
        pc = conv_map.get(b.symbol)
        conv = pc.conviction if pc else (row.actionability_score if row else 50.0)
        sector = pc.sector if pc else (row.sector if row else "—")
        lv = apply_levels(entry, atr, calib) if (use_calib and atr) else None
        if lv:
            # STRUCTURE-AWARE STOP. The calibrated stop is a pure volatility floor
            # (k×ATR — "keeps ~85% of winners in") but it is blind to chart structure,
            # so it can sit just above an obvious swing low and get wicked on a normal
            # support test. Reconcile with the engine's structural invalidation level
            # (below swing low / EMA20 / base): take the SAFER (lower) of the two so
            # the stop clears BOTH noise AND structure, then cap total per-share risk.
            atr_stop = lv["stop"]
            struct_stop = (row.stop if (row and row.stop and row.stop > 0)
                           else b.stop_price)
            stop = (min(atr_stop, struct_stop)
                    if (struct_stop and struct_stop > 0) else atr_stop)
            floor_price = entry * (1 - _MAX_STOP_PCT / 100.0)   # never risk > cap/share
            if stop < floor_price:
                stop = round(floor_price, 2)
                if struct_stop and 0 < struct_stop < atr_stop:
                    warn = warn or (f"stop risk-capped at {_MAX_STOP_PCT:.0f}% "
                                    f"(structural support is deeper — size kept small)")
            elif struct_stop and 0 < struct_stop < atr_stop:
                warn = warn or ("stop at structural support (below swing low/EMA20) — "
                                "wider than the ATR floor, won't wick on a support test")
            t1, t2 = lv["t1"], lv["t2"]
            # conviction-scaled risk budget at the ADAPTIVE risk/trade (edge-health)
            budget = (conv / 100.0) * (eff_risk / 100.0) * cap
            rps = max(entry - stop, 1e-9)
            qty = min(int(budget / rps), int(0.25 * cap / entry) if entry else 10**9)
        else:
            stop, t1, t2, qty = b.stop_price, b.target1, b.target2, b.quantity

        # ── TRADE QUALITY INTELLIGENCE (Phase O) — risk-manager veto/sizer ───────
        #    Eliminates fragile stops, FOMO/crowded entries, death-zone & liquidity-
        #    trap setups, and weak structure. Reads price/volume STRUCTURE only (no
        #    new indicators) + the system's own validated analogs. Rejects → WAIT.
        tq = None
        df_q = res.feed.data.get(b.ticker)
        if lv and _assess_trade and df_q is not None and len(df_q) >= 60:
            try:
                cohort = (_lookup_cohort(path_cache, res.regime.regime, row.rs_status, atr_pct)
                          if (path_cache and atr_pct and _lookup_cohort) else None)
            except Exception:
                cohort = None
            try:
                expected = (_expected_move(stats, row.rs_status, row.classification)
                            if (stats and _expected_move) else None)
            except Exception:
                expected = None
            try:
                tq = _assess_trade(
                    df_q, entry=entry, stop=stop, atr=(atr or atr_raw), t1=t1, t2=t2,
                    entry_context=row.entry_context, extension_pct=row.extension_pct,
                    rs_status=row.rs_status, vol_ctx=_volume_context(df_q),
                    regime=res.regime.regime, breadth=res.regime.breadth_score,
                    participation=res.regime.participation_score,
                    leadership=res.regime.leadership_score,
                    sector_grade=sector_grade_map.get(sector),
                    earnings_days=_earnings_days(b.ticker),
                    cohort=cohort, expected=expected,
                    calib_p1=lv.get("p_t1"), calib_p2=lv.get("p_t2"))
            except Exception:
                tq = None
            if tq is not None:
                # NEVER hide a genuine entry — adopt the safer (quality-improved) stop
                # and a quality-scaled size (smaller for lower quality, floored so it
                # stays actionable). The trader makes the final call; we de-risk it.
                stop = tq.stop
                budget = (conv / 100.0) * (eff_risk / 100.0) * cap
                rps = max(entry - stop, 1e-9)
                qty = min(int(budget / rps * tq.size_factor),
                          int(0.25 * cap / entry) if entry else 10**9)
                if tq.size_factor < 1.0:
                    warn = warn or f"size ×{tq.size_factor:.2f} — quality risk control"

        if not (qty and qty > 0):
            return None
        return {"sym": b.symbol, "sector": sector, "conv": conv, "tier": _tier(conv),
                "qty": qty, "entry": entry, "stop": stop, "t1": t1, "t2": t2,
                "risk": max(entry - (stop or entry), 0) * qty, "lv": lv,
                "row": row, "warn": warn, "tq": tq}

    sized_fail: set[str] = set()      # names whose risk budget sized to 0 shares
    cands = []
    for b, row in buy_now:
        c = _sized_candidate(b, row)
        if c:
            cands.append(c)
        elif row is not None:
            sized_fail.add(row.ticker)

    # PORTFOLIO RISK: new-position budget from the ONE config truth —
    # min(max_new_per_week, target_positions − already held). Conviction
    # concentrates; never diversify for its own sake. The caps are shared with
    # the conditional orders below so the whole plan can never imply more than
    # MAX_POSITIONS fresh names.
    MAX_POSITIONS = max(0, min(max_new, target_pos - len(state.holdings)))
    cands.sort(key=lambda c: -c["conv"])
    selected, deferred, sec_count = [], [], {}
    for c in cands:
        if len(selected) >= MAX_POSITIONS:
            c["defer_why"] = f"committee cap: max {MAX_POSITIONS} positions"
            deferred.append(c)
        elif sec_count.get(c["sector"], 0) >= SECTOR_CAP:
            c["defer_why"] = f"sector cap: {c['sector']} full"
            deferred.append(c)
        else:
            selected.append(c)
            sec_count[c["sector"]] = sec_count.get(c["sector"], 0) + 1

    # ── ARMED leaders → EXACT CONDITIONAL ORDERS (the WAIT-killer). A proven
    #    leader setup with no confirmation bar yet gets a GTT buy-stop AT its
    #    trigger price, fully sized with the same calibrated stop/targets — so
    #    "wait" leaves nothing to decide: place the order; the market decides.
    armed_pairs = [(b, row) for b, row in wait
                   if _is_armed(row, res.leader_weeks, confirm.get(b.ticker))]
    # Rank by the VALIDATED criteria only: market leadership first (the proven
    # cohort), then composite strength (Top-10 basket, +3.21% expectancy).
    # Never by RR / analog-EV — both failed OOS.
    armed_pairs.sort(key=lambda br: (0 if br[1].rs_status == "MARKET_LEADER" else 1,
                                     -br[1].composite_score))
    conditionals = []
    for b, row in armed_pairs:
        if len(selected) + len(conditionals) >= MAX_POSITIONS:
            break
        if sec_count.get(row.sector, 0) >= SECTOR_CAP:
            continue
        cf = confirm.get(b.ticker, {})
        if row.entry_context == "BREAKOUT_SETUP":
            base = cf.get("pivot") or row.entry
            trig, kind = round(base * 1.001, 2), "BUY ON BREAKOUT"
        else:
            trig, kind = round(row.entry * 1.005, 2), "BUY ON PULLBACK"
        c = _sized_candidate(b, row, entry_override=trig)
        if not c:
            sized_fail.add(row.ticker)
            continue
        c.update({"kind": kind, "trigger": trig, "limit": round(trig * 1.003, 2),
                  "cf_why": cf.get("why", "")})
        conditionals.append(c)
        sec_count[row.sector] = sec_count.get(row.sector, 0) + 1

    # Split the visible book into PRIME (take with confidence) vs lower-quality
    # (your call, smaller size). SHOW EVERYTHING — nothing is filtered out; the
    # trader decides. Quality has already pre-sized the lower-grade names down.
    prime    = [c for c in selected if c.get("tq") and c["tq"].recommended]
    marginal = [c for c in selected if not (c.get("tq") and c["tq"].recommended)]

    # ── ② COMMITTEE DECISION — ONE unambiguous verdict, then the exact orders.
    #    The verdict and the sections below can never disagree: BUY NOW is said
    #    only when a PRIME order actually follows (the old code said "BUY NOW —
    #    execute below" and then printed "NO PRIME SETUP TODAY" beneath it).
    if prime:
        verdict, vcol = "BUY NOW", G
        vwhy = (f"{len(prime)} prime setup(s) printed their confirmation bar on the last "
                f"complete session — execute the order card below, then stop thinking")
    elif selected:
        verdict, vcol = "BUY — LOWER QUALITY (YOUR CALL)", Y
        vwhy = (f"{len(selected)} confirmed trigger(s), but none is A/B-grade PRIME — "
                f"risk is surfaced and size is pre-cut below; take or skip is the one "
                f"call the committee leaves to you")
    elif conditionals:
        kinds = sorted({c["kind"] for c in conditionals})
        verdict = kinds[0] if len(kinds) == 1 else "WAIT FOR TRIGGER"
        vcol = Y
        vwhy = (f"proven leader setup(s), confirmation bar not printed yet — place the "
                f"{len(conditionals)} conditional GTT(s) below and walk away: a fill IS "
                f"the trigger; no fill = no trade = no loss")
    elif MAX_POSITIONS == 0 and (cands or armed_pairs):
        verdict, vcol = "BOOK FULL — NO NEW SLOTS", Y
        vwhy = (f"{len(state.holdings)} positions held vs target_positions={target_pos} "
                f"(config/deployment.yaml) — qualified candidates exist but the book has "
                f"no room; manage exits first, the trace below names what's queued")
    else:
        verdict, vcol = "STAY IN CASH", R
        vwhy = "nothing armed, nothing confirmed — numerical proof below, cash is the position"
    stale_tag = "" if live else f"   {R}⚠ stale session — --kite-login before placing{RST}"
    print(f"\n  {vcol}{B}  ▶ COMMITTEE DECISION: {verdict}{RST}{stale_tag}")
    print(f"  {D}    {vwhy}{RST}")

    if selected:
        heat = sum(c["risk"] for c in selected) / cap * 100
        sec_val = {}
        for c in selected:
            sec_val[c["sector"]] = sec_val.get(c["sector"], 0) + c["qty"] * c["entry"]
        lsec, lval = max(sec_val.items(), key=lambda kv: kv[1])
        hc = R if heat > MAX_BOOK_HEAT else G

        if prime:
            print(f"\n  {G}{B}  ② BUY TODAY: {len(prime)} prime setup(s){RST}  {D}({note}){RST}")
        else:
            print(f"\n  {Y}{B}  ② NO PRIME SETUP TODAY{RST}  "
                  f"{D}(nothing A/B-grade — only lower-quality options below, your call){RST}")
        print(f"  {D}  PORTFOLIO RISK (if you take all shown): book heat {hc}{heat:.1f}%{RST}{D} "
              f"of capital  ·  largest sector {lsec} {lval/cap*100:.0f}% "
              f"({sec_count.get(lsec,0)}/{SECTOR_CAP} cap)  ·  {len(selected)} on the table "
              f"({len(prime)} prime · {len(marginal)} lower-quality) · max {MAX_POSITIONS} positions{RST}")
        if heat > MAX_BOOK_HEAT:
            print(f"  {R}  ⚠ Book heat {heat:.1f}% > {MAX_BOOK_HEAT:.0f}% cap if you take "
                  f"everything — prefer the prime names / size the rest down{RST}")

        if prime:
            _buy_table_header()
            for i, c in enumerate(prime, 1):
                _render_buy_line(i, c, cap, eff_risk, color=G)

        if marginal:
            print(f"\n  {Y}{B}  ②b ALSO ON THE TABLE — lower quality, smaller size (your call){RST}"
                  f"  {D}(shown on purpose — risk is surfaced + pre-sized down; you decide){RST}")
            _buy_table_header()
            for i, c in enumerate(marginal, 1):
                _render_buy_line(i, c, cap, eff_risk, color=Y)

    # ── ③ CONDITIONAL ORDERS — armed leaders as exact, pre-sized GTT buy-stops.
    #    This kills "a month of WAIT": waiting is now an ORDER, not a feeling.
    if conditionals:
        print(f"\n  {Y}{B}  ③ CONDITIONAL ORDERS — place these GTTs now, they execute "
              f"themselves{RST}  {D}({note}){RST}")
        for i, c in enumerate(conditionals, 1):
            lv = c["lv"] or {}
            entry = c["entry"]

            def _lvl(p):
                return (f"{_fmt_money(p)} ({(p / entry - 1) * 100:+.1f}%)" if p else "—")

            print(f"\n  {Y}  {i}  {c['sym'][:12]:12s}  {c['kind']}  ·  tier {c['tier']} "
                  f"(conv {c['conv']:.0f})  ·  {c['sector']}{RST}")
            print(f"  {D}       order:  GTT BUY  trigger {_fmt_money(c['trigger'])} · "
                  f"limit {_fmt_money(c['limit'])} (max slippage 0.3%) · qty {c['qty']} "
                  f"≈ {_fmt_money(c['qty'] * entry)} · risk ₹{c['risk']:,.0f} "
                  f"({c['risk'] / cap * 100:.2f}% of equity){RST}")
            t1_tail = (f" · {lv.get('p_t1', 0) * 100:.0f}% hit ~{lv.get('days_t1', '?')}d"
                       if lv else "")
            t2_tail = (f" · {lv.get('p_t2', 0) * 100:.0f}% hit ~{lv.get('days_t2', '?')}d"
                       if lv else "")
            print(f"  {D}       levels: stop {_lvl(c['stop'])} · T1 {_lvl(c['t1'])}{t1_tail} "
                  f"· T2 {_lvl(c['t2'])}{t2_tail}{RST}")
            if lv:
                print(f"  {D}       hold:   expected ~{lv.get('days_t1', '?')}–"
                      f"{lv.get('days_t2', '?')}d after fill · hard time-stop: exit "
                      f"day 20 if T1 not hit{RST}")
            print(f"  {D}       stop basis: structural support (swing low/EMA20) reconciled "
                  f"with the 3×ATR noise floor — ~85% of winning analogs never touch it{RST}")
            print(f"  {D}       status: {c['cf_why']}{RST}")
            print(f"  {D}       cancel-if: closes below stop before filling · unfilled after "
                  f"5 sessions · regime turns BEAR/VOLATILE at the weekly screen{RST}")
            _render_trade_quality(c.get("tq"))
            if c["warn"]:
                print(f"  {Y}       ⚠ {c['warn']}{RST}")
            try:
                from scanner.earnings_calendar import earnings_warning
                ew = earnings_warning(f"{c['sym']}.NS")
                if ew:
                    print(f"  {R}       {ew}{RST}")
            except Exception:
                pass

    # ── STAY IN CASH must be EARNED — wait-policy proof with measured values ──
    #    (skipped when the blocker is a full book — that proof would be false)
    if not selected and not conditionals and not (MAX_POSITIONS == 0
                                                  and (cands or armed_pairs)):
        _render_cash_proof(res, edge_h)

    # one mechanical exit policy for everything above — zero discretion left
    if selected or conditionals:
        _render_exit_rules(calib)

    # ④ DECISION TRACE — the full audit chain from board to plan. ONE line per
    #    name: board rank → scores → gate/trigger outcome → final plan status,
    #    printed from the SAME evaluation that gated the trade (lifecycle
    #    decide()), never a re-derived approximation. Covers every visible board
    #    name AND every name the plan touched (even if it ranks below the board
    #    cut) — so "why did X appear / why did Y lose" is never a mystery.
    plan_state: dict[str, str] = {}
    for i, c in enumerate(selected, 1):
        tag = ("PRIME BUY" if (c.get("tq") and c["tq"].recommended)
               else "BUY, lower quality")
        plan_state[c["row"].ticker] = (f"{G}SELECTED #{i}{RST}{D} — {tag} · qty "
                                       f"{c['qty']} @ {_fmt_money(c['entry'])}{RST}")
    for i, c in enumerate(conditionals, 1):
        plan_state[c["row"].ticker] = (f"{Y}CONDITIONAL #{i}{RST}{D} — GTT trigger "
                                       f"{_fmt_money(c['trigger'])} · qty {c['qty']}{RST}")
    for c in deferred:
        plan_state[c["row"].ticker] = (f"{Y}DEFERRED{RST}{D} — "
                                       f"{c.get('defer_why', 'capped')} (conv "
                                       f"{c['conv']:.0f} ranked lower){RST}")

    alloc_skips = (snap.metrics or {}).get("allocator_skips", {})
    board_rows  = res.act_snap.top_n(10)
    trace_ticks = {r.ticker for r in board_rows}
    extra_ticks = ({t for t in plan_state}
                   | {row.ticker for _, row in buy_now if row is not None}
                   | {row.ticker for _, row in wait if row is not None}) - trace_ticks
    extra_rows  = [res.act_snap.get(t) for t in extra_ticks]
    trace_rows  = board_rows + sorted((r for r in extra_rows if r is not None),
                                      key=lambda r: r.rank)

    print(f"\n  {B}  ④ DECISION TRACE — why every candidate won or lost{RST}  "
          f"{D}(board → gates → trigger → plan; nothing here needs a decision){RST}")
    for r in trace_rows[:18]:
        cf = confirm.get(r.ticker)
        if cf is None:
            cf = _entry_confirmation(r, res.feed.data.get(r.ticker))
        dec = _lc_decide(r, res.leader_weeks, cf)
        if r.ticker in plan_state:
            trig = (cf.get("why", "") if (cf.get("breakout") or cf.get("reclaim"))
                    else ("ACTION_NOW" if r.classification == "ACTION_NOW"
                          else "awaiting trigger"))
            status = f"{plan_state[r.ticker]}{D} · {trig}{RST}"
        elif not dec.armed and not dec.triggered:
            status = f"{R}REJECTED{RST}{D} — {dec.reason}{RST}"
        elif dec.triggered:
            if r.ticker in sized_fail:
                why = ("risk budget sizes to 0 shares at current equity "
                       "(share price too high for the ₹ risk/trade)")
            else:
                why = alloc_skips.get(r.symbol, "not in the target portfolio "
                                                "(allocator pool/caps)")
            status = f"{Y}TRIGGERED, NOT SIZED{RST}{D} — {why}{RST}"
        else:
            # ARMED: the reason a trader needs is WHAT the trigger is waiting
            # for, not allocator internals — those go in parentheses if binding.
            if r.ticker in sized_fail:
                why = ("risk budget sizes to 0 shares at current equity "
                       "(share price too high for the ₹ risk/trade)")
            else:
                why = cf.get("why") or dec.reason
                slots_full = len(selected) + len(conditionals) >= MAX_POSITIONS
                tail = alloc_skips.get(r.symbol, "no conditional slot free"
                                       if slots_full else "")
                if tail:
                    why = f"{why} ({tail})"
            status = f"{Y}ARMED — NO ORDER{RST}{D} — {why}{RST}"
        print(f"  {D}  #{r.rank:>2} {r.symbol[:12]:<12s} act {r.actionability_score:>4.0f} · "
              f"comp {r.composite_score:>4.0f} · {r.rs_status.replace('_', ' ').lower():<15s} "
              f"→ {RST}{status}")

    if card.holds:
        print(f"\n  {D}  HOLD: {' · '.join(h.symbol for h in card.holds[:8])}{RST}")

    # cash check against REAL available cash (confirmed buys + conditional fills)
    if selected or conditionals:
        spend_prime = sum(c["qty"] * c["entry"] for c in prime)
        spend_cond  = sum(c["qty"] * c["entry"] for c in conditionals)
        spend_marg  = sum(c["qty"] * c["entry"] for c in marginal)
        committed   = spend_prime + spend_cond          # prime buys + GTT fills
        after       = state.available_cash - committed
        after_all   = after - spend_marg
        cc  = G if after >= 0 else R
        ca  = G if after_all >= 0 else R
        extra = (f"  |  + lower-quality {_fmt_money(spend_marg)} → cash "
                 f"{ca}{_fmt_money(after_all)}{RST}" if marginal else "")
        print(f"\n  {D}  Committed cost (prime + GTT fills): {_fmt_money(committed)}  |  "
              f"Cash after: {cc}{_fmt_money(after)}{RST}{D}{extra}{RST}")
        worst = after_all if marginal else after
        if worst < 0:
            print(f"  {R}  ⚠  Short by {_fmt_money(abs(worst))} if everything fills — "
                  f"drop the lowest-conviction name or add funds{RST}")
        print(f"  {G}  Execute:  python run.py --place TICKER   (GTT from today's plan) · "
              f"then --positions after any fill{RST}")

    # snapshot context so executed fills get journaled with full decision context.
    # ALWAYS write (even empty) — a dated file is the staleness guard for --place
    # and sync enrichment; conditional GTTs may fill days later and need context.
    if _snapshot_plan:
        _snapshot_plan(selected + conditionals, res.regime.regime,
                       (edge_h or {}).get("verdict", "?"), rm["mode"])

    _render_live_journal(journal_stats)
    _render_edge_health(edge_h)
    _render_evidence_footer(stats)


def render_cockpit(res: ScreenResult, research: bool = False) -> None:
    """Decision-first cockpit. Default = only what feeds TODAY's decision:
    data quality → regime → sectors → top-10 board → actionable alerts →
    ZERO-DECISION PLAN (verdict + exact orders + exit rules).
    `research=True` restores the analyst tables (top-20 board, extended monitor
    list, adaptive scoring promotions/demotions)."""
    today = date.today().isoformat()
    print(f"\n{B}{'═'*100}{RST}")
    print(f"{B}  SWING TRADING COCKPIT  ·  {today}  ·  "
          f"universe={res.universe.symbol_count} ({res.universe.source})  "
          f"data={res.feed.coverage*100:.0f}%{RST}")
    print(f"{B}{'═'*100}{RST}")

    # 0) Data quality (benchmark resilience) — surfaced BEFORE regime, because a
    #    missing benchmark makes the regime/leadership read untrustworthy.
    _render_data_quality(res.data_quality)

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
    # G6: show adaptive weights as a compact one-liner — the full table was noise
    if res.adaptive_snap and res.adaptive_snap.weights:
        wt = "  ".join(f"{_LABEL_SHORT.get(k,k)} {int(v)}"
                       for k, v in sorted(res.adaptive_snap.weights.items(),
                                          key=lambda kv: -kv[1]))
        print(f"  {D}Adaptive weights (this regime):  {wt}{RST}")

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

    # Pre-compute volume context for all top-40 candidates (display overlay — no scoring)
    vol_ctx: dict[str, dict] = {}
    for _r in res.act_snap.top_n(40):
        _df = res.feed.data.get(_r.ticker)
        if _df is not None:
            vol_ctx[_r.ticker] = _volume_context(_df)

    # 3) Candidate board — top 10 by default (the decision only ever uses the
    #    top of the board); --research restores the full top 20.
    board_n = 20 if research else 10
    cd = res.act_snap.classification_distribution()
    top20 = res.act_snap.top_n(board_n)
    dist_parts = []
    for cls in ("ACTION_NOW", "WATCHLIST", "EXTENDED", "AVOID"):
        cnt = sum(1 for r in top20 if r.classification == cls)
        if cnt:
            dist_parts.append(f"{cnt} {cls}")
    dist_str = "  ·  ".join(dist_parts)
    print(f"\n  {B}TOP {board_n} CANDIDATE BOARD{RST}   {D}{dist_str}{RST}")
    print(f"  {D}{'#':>2}  {'TICKER':12s}  {'COMP':>5}  {'ACT':>5}  {'GR':>3}  "
          f"{'CLASS':10s}  {'RR':>4}  {'RVOL':>4}  {'ENTRY ZONE':21s}  {'STOP':>9}  DRIVERS{RST}")
    print(f"  {D}{'─'*150}{RST}")
    for r in top20:
        cc = _class_color(r.classification)
        gc = G if r.grade in ("A+", "A") else (Y if r.grade in ("B+", "B") else "")
        drv = " · ".join(r.drivers[:3])
        rv = _rvol_fmt(vol_ctx.get(r.ticker, {}))
        print(f"  {r.rank:>2}  {r.symbol[:12]:12s}  {r.composite_score:>5.1f}  "
              f"{r.actionability_score:>5.1f}  {gc}{r.grade:>3}{RST}  "
              f"{cc}{r.classification:10s}{RST}  {r.rr_ratio:>4.1f}  "
              f"{rv}  {r.entry_zone:21s}  {r.stop_zone:>9}  {drv}")
    print(f"  {D}  ⓘ Board ENTRY/STOP/RR are raw scan geometry (ranking context only — "
          f"RR carries zero weight). EXECUTION levels — calibrated stop/T1/T2 and exact "
          f"qty — are in the trade plan below. Always trade the plan.{RST}")

    # 4) (removed) — the single actionable verdict now lives in ZERO-DECISION TRADE
    #    PLAN below (BUY TODAY / NO TRADE TODAY). The legacy "TOP 5 ACTIONABLE /
    #    No ACTION_NOW" block contradicted it, so it was deleted: one truth only.

    # 5) G9: EXTENDED class — research table (strongest IS, excluded from buys).
    #    Hidden by default: it is monitoring context, not a decision input. The
    #    actionable version (volume-confirmed pullback alert, 5b) always shows.
    extended = [r for r in res.act_snap.top_n(40) if r.classification == "EXTENDED"]
    if extended and research:
        print(f"\n  {B}{'─'*60}{RST}")
        print(f"  {B}  EXTENDED SETUPS — monitor for pullback entry{RST}   "
              f"{D}(IS evidence: t=8.41, +3.85% 20D avg — strongest class){RST}")
        print(f"  {D}  Not in buy list (OOS unvalidated). Alert = wait for base/pullback to support.{RST}")
        for r in extended[:5]:
            vc = vol_ctx.get(r.ticker, {})
            rvol_s = f"  rvol {vc['rvol']:.1f}x" if vc.get("rvol") is not None else ""
            pb_s = f"  pullback {vc['pullback_pct']:.1f}% off high" if vc.get("pullback_pct") is not None else ""
            print(f"  {D}  {r.symbol:<12s}  composite {r.composite_score:.1f}"
                  f"{rvol_s}{pb_s}  entry {r.entry_zone}  stop {r.stop_zone}{RST}")

    # 5b) EXTENDED PULLBACK ALERTS — volume-confirmed pullback to support (highest-conviction watch)
    ep = [(r, vol_ctx.get(r.ticker, {}))
          for r in res.act_snap.top_n(40)
          if r.classification == "EXTENDED" and _is_extended_pullback(vol_ctx.get(r.ticker, {}))]
    if ep:
        print(f"\n  {B}{'─'*60}{RST}")
        print(f"  {G}{B}  ⚡ EXTENDED PULLBACK ALERTS — entry window may be re-opening{RST}")
        print(f"  {D}  Why: EXTENDED is your only statistically proven class (t=8.41, +3.85% 20D avg).{RST}")
        print(f"  {D}  Signal: pulled back 3-12% from 20D high on DRYING volume + holding EMA20.{RST}")
        print(f"  {Y}  Research only (pullback-within-EXTENDED is unvalidated). "
              f"Confirm with 1 up-day before entry.{RST}")
        for r, vc in ep[:5]:
            pb  = vc.get("pullback_pct", 0)
            vt  = vc.get("vol_trend_3d", 1.0)
            sup = "above EMA20" if vc.get("above_ema20") else "near EMA20"
            rv  = vc.get("rvol", 0)
            print(f"  {G}  {r.symbol:<12s}  pullback {pb:.1f}% off high  "
                  f"vol {vt:.2f}x (3D avg, drying)  today {rv:.1f}x  {sup}  "
                  f"entry {r.entry_zone}  stop {r.stop_zone}{RST}")

    # 6) Adaptive (regime-aware) scoring — research table only. The regime block
    #    already shows the active weights one-liner; the rank-delta/promotion/
    #    demotion tables changed no decision and were pure noise on a trade day.
    if research:
        _render_adaptive(res.adaptive_snap)

    # 7) Zero-decision trade plan — verdict + exact orders + mechanical exits
    _render_zero_decision_plan(res)
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
def render_benchmark_abort(exc: "BenchmarkUnavailableError") -> None:
    """Fail-loud abort screen: no regime, no board — the benchmark is gone."""
    print(f"\n  {R}{'═'*72}{RST}")
    print(f"  {R}{B}  ^NSEI UNAVAILABLE — SCREEN ABORTED{RST}")
    print(f"  {R}{'═'*72}{RST}")
    print(f"  {Y}  Reason: market benchmark unavailable (live download AND cache both failed).{RST}")
    print(f"  {Y}          Leadership / relative-strength / regime analysis would be invalid.{RST}")
    print(f"  {D}  No regime. No ACTION_NOW. No board. Re-run when data returns.{RST}")
    bs = getattr(exc, "benches", {}) or {}
    if bs:
        cells = "   ".join(f"{k} {_bench_color(v)}{v.upper()}{RST}" for k, v in bs.items())
        print(f"  {D}  Benchmarks:{RST} {cells}")
    print(f"  {D}  (Override for research only: build_screen(allow_degraded=True) "
          f"→ sector-relative fallback.){RST}")
    print(f"  {R}{'═'*72}{RST}\n")


def run_screen(period: str = "1y", force_refresh: bool = False,
               research: bool = False) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(f"\n  {D}Building swing screen (universe → sector → RS → composite → "
          f"actionability)...{RST}")
    try:
        res = build_screen(period=period, force_refresh=force_refresh, persist=True)
    except BenchmarkUnavailableError as exc:
        render_benchmark_abort(exc)
        return 2
    render_cockpit(res, research=research)
    paths = export_reports(res)
    print(f"  {D}Exports: {paths['json']} · {paths['csv']} · {paths['md']}{RST}"
          f"{'' if research else f'  ·  full analyst tables: --screen --research'}\n")
    return 0
