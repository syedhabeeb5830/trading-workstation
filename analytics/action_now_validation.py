"""
analytics/action_now_validation.py — 5-Year Forensic Classification Validation
==============================================================================
RESEARCH / MEASUREMENT ONLY. Replays the screener EXACTLY as it exists today over
a multi-year weekly history and measures forward returns by classification, score
decile, regime, grade and RS bucket. It changes NO scoring, thresholds, regime,
ranking or factors — it only reuses the production engines through time (via
analytics.edge_validation._replay_as_of) and tabulates what happened next.

Output: reports/validation/action_now_5y.md

    python -m analytics.action_now_validation [years] [every_weeks] [sample]

Cadence note: uses the WEEKLY production cadence (the cadence the system actually
runs / persists), not every calendar day — daily replay adds heavy autocorrelation
without new information.
"""
from __future__ import annotations

import statistics as st
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from analytics.edge_validation import ForwardReturnEngine
from analytics.rolling_validation import generate_rolling_records, _welch_t

HORIZONS = [5, 10, 20, 60, 120]
PRIMARY = "20"
_OUT = Path("reports/validation")


# ─────────────────────────────────────────────────────────────────────────────
# STAT HELPERS (no scipy)
# ─────────────────────────────────────────────────────────────────────────────
def _clean(xs) -> list[float]:
    return [x for x in xs if x is not None]


def _mean(xs) -> Optional[float]:
    xs = _clean(xs)
    return round(sum(xs) / len(xs), 2) if xs else None


def _median(xs) -> Optional[float]:
    xs = _clean(xs)
    return round(st.median(xs), 2) if xs else None


def _win(xs) -> Optional[float]:
    xs = _clean(xs)
    return round(sum(1 for x in xs if x > 0) / len(xs), 3) if xs else None


def _sharpe(xs) -> Optional[float]:
    xs = _clean(xs)
    if len(xs) < 2:
        return None
    sd = st.pstdev(xs)
    return round(_mean(xs) / sd, 2) if sd else None


def _fmt(v, suffix="") -> str:
    return "—" if v is None else f"{v}{suffix}"


def _grp_stats(returns, mae=None) -> dict:
    """Per-group summary: count, win, avg, median, sharpe (+ avg/worst drawdown via MAE)."""
    out = {"n": len(_clean(returns)), "win": _win(returns), "avg": _mean(returns),
           "median": _median(returns), "sharpe": _sharpe(returns)}
    if mae is not None:
        cm = _clean(mae)
        out["avg_mae"] = round(sum(cm) / len(cm), 2) if cm else None     # typical drawdown
        out["worst_mae"] = round(min(cm), 2) if cm else None             # max drawdown
    return out


# ─────────────────────────────────────────────────────────────────────────────
# EXCURSIONS (max favourable / adverse over the 20D swing window)
# ─────────────────────────────────────────────────────────────────────────────
def _attach_excursions(records, ohlcv, window: int = 20) -> None:
    for r in records:
        df = ohlcv.get(r.ticker)
        if df is None or df.empty:
            continue
        pos = int(df.index.searchsorted(pd.Timestamp(r.screen_date), side="right")) - 1
        if pos < 0 or pos >= len(df):
            continue
        base = float(df["Close"].iloc[pos])
        if base <= 0:
            continue
        seg = df.iloc[pos + 1: min(pos + window, len(df) - 1) + 1]
        if seg.empty:
            continue
        hi = float(seg["High"].max()) if "High" in seg else float(seg["Close"].max())
        lo = float(seg["Low"].min()) if "Low" in seg else float(seg["Close"].min())
        r.fwd["mfe20"] = round((hi / base - 1) * 100, 2)   # max gain
        r.fwd["mae20"] = round((lo / base - 1) * 100, 2)   # max drawdown


# ─────────────────────────────────────────────────────────────────────────────
# AGGREGATIONS
# ─────────────────────────────────────────────────────────────────────────────
def _by(records, keyfn):
    g: dict[str, list] = {}
    for r in records:
        k = keyfn(r)
        if k is not None:
            g.setdefault(k, []).append(r)
    return g


def _ret(recs, h):
    return [r.fwd.get(h) for r in recs]


def _mae(recs):
    return [r.fwd.get("mae20") for r in recs]


def _decile(score: float) -> Optional[str]:
    if score is None:
        return None
    if score >= 90: return "90-100"
    if score >= 80: return "80-89"
    if score >= 70: return "70-79"
    if score >= 60: return "60-69"
    if score >= 50: return "50-59"
    return "<50"


def _corr(pairs) -> Optional[float]:
    xs = [(a, b) for a, b in pairs if a is not None and b is not None]
    if len(xs) < 3:
        return None
    a = [p[0] for p in xs]; b = [p[1] for p in xs]
    sa, sb = st.pstdev(a), st.pstdev(b)
    if sa == 0 or sb == 0:
        return None
    ma, mb = st.mean(a), st.mean(b)
    cov = sum((x - ma) * (y - mb) for x, y in xs) / len(xs)
    return round(cov / (sa * sb), 3)


# ─────────────────────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────────────────────
def _tbl(header: list[str], rows: list[list]) -> str:
    L = ["| " + " | ".join(header) + " |",
         "|" + "|".join(["---"] * len(header)) + "|"]
    for row in rows:
        L.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(L)


def build_report(records, n_dates, drange) -> str:
    n = len(records)
    CLASS = ["ACTION_NOW", "WATCHLIST", "EXTENDED", "AVOID"]
    by_class = _by(records, lambda r: r.classification)
    uni20 = _mean(_ret(records, PRIMARY))

    # ── classification: detailed 20D + multi-horizon avg ─────────────────────
    cls_detail = []
    for c in CLASS:
        recs = by_class.get(c, [])
        s = _grp_stats(_ret(recs, PRIMARY), _mae(recs))
        cls_detail.append([c, s["n"], _fmt(s["win"]), _fmt(s["avg"], "%"),
                           _fmt(s["median"], "%"), _fmt(s["avg_mae"], "%"),
                           _fmt(s["worst_mae"], "%"), _fmt(s["sharpe"])])
    cls_horizon = []
    for c in CLASS:
        recs = by_class.get(c, [])
        cls_horizon.append([c] + [_fmt(_mean(_ret(recs, str(h)), ), "%") for h in HORIZONS])

    # ── score deciles (20D) ──────────────────────────────────────────────────
    by_dec = _by(records, lambda r: _decile(r.composite_score))
    dec_rows = []
    for d in ["90-100", "80-89", "70-79", "60-69", "50-59", "<50"]:
        recs = by_dec.get(d, [])
        if not recs:
            continue
        s = _grp_stats(_ret(recs, PRIMARY), _mae(recs))
        dec_rows.append([d, s["n"], _fmt(s["win"]), _fmt(s["avg"], "%"),
                         _fmt(s["median"], "%"), _fmt(s["avg_mae"], "%")])

    # ── actionability quintiles (Q6) ─────────────────────────────────────────
    act_q = _by(records, lambda r: ("80-100" if r.actionability_score >= 80 else
                                    "60-79" if r.actionability_score >= 60 else
                                    "40-59" if r.actionability_score >= 40 else "<40"))
    actq_rows = []
    for q in ["80-100", "60-79", "40-59", "<40"]:
        recs = act_q.get(q, [])
        if recs:
            actq_rows.append([q, len(recs), _fmt(_win(_ret(recs, PRIMARY))),
                              _fmt(_mean(_ret(recs, PRIMARY)), "%")])
    act_corr = _corr([(r.actionability_score, r.fwd.get(PRIMARY)) for r in records])
    comp_corr = _corr([(r.composite_score, r.fwd.get(PRIMARY)) for r in records])

    # ── regime (20D) + regime × ACTION_NOW ───────────────────────────────────
    by_reg = _by(records, lambda r: r.regime)
    reg_rows, regan_rows = [], []
    for rg in sorted(by_reg, key=lambda k: -_mean(_ret(by_reg[k], PRIMARY)) if _mean(_ret(by_reg[k], PRIMARY)) is not None else 0):
        recs = by_reg[rg]
        s = _grp_stats(_ret(recs, PRIMARY), _mae(recs))
        reg_rows.append([rg, s["n"], _fmt(s["win"]), _fmt(s["avg"], "%"), _fmt(s["avg_mae"], "%")])
        an = [r for r in recs if r.classification == "ACTION_NOW"]
        sa = _grp_stats(_ret(an, PRIMARY), _mae(an))
        regan_rows.append([rg, sa["n"], _fmt(sa["win"]), _fmt(sa["avg"], "%"), _fmt(sa["avg_mae"], "%")])

    # ── grade + RS (20D) ─────────────────────────────────────────────────────
    by_grade = _by(records, lambda r: r.grade)
    grade_rows = []
    for g in ["A+", "A", "B+", "B", "C", "D"]:
        recs = by_grade.get(g, [])
        if recs:
            grade_rows.append([g, len(_clean(_ret(recs, PRIMARY))), _fmt(_win(_ret(recs, PRIMARY))),
                               _fmt(_mean(_ret(recs, PRIMARY)), "%"), _fmt(_median(_ret(recs, PRIMARY)), "%")])
    by_rs = _by(records, lambda r: r.rs_status)
    rs_rows = []
    for s in ["MARKET_LEADER", "SECTOR_LEADER", "EMERGING_LEADER", "NEUTRAL", "LAGGARD"]:
        recs = by_rs.get(s, [])
        if recs:
            rs_rows.append([s, len(_clean(_ret(recs, PRIMARY))), _fmt(_win(_ret(recs, PRIMARY))),
                            _fmt(_mean(_ret(recs, PRIMARY)), "%")])

    # ── the 9 questions ──────────────────────────────────────────────────────
    def grp(c): return by_class.get(c, [])
    def reg_grp(names): return [r for r in records if r.regime in names]
    an20 = _ret(grp("ACTION_NOW"), PRIMARY)
    av20 = _ret(grp("AVOID"), PRIMARY)
    wl20 = _ret(grp("WATCHLIST"), PRIMARY)
    ex20 = _ret(grp("EXTENDED"), PRIMARY)
    ml20 = _ret(by_rs.get("MARKET_LEADER", []), PRIMARY)
    sl20 = _ret(by_rs.get("SECTOR_LEADER", []), PRIMARY)
    ap20 = _ret(by_grade.get("A+", []), PRIMARY)
    a20 = _ret(by_grade.get("A", []), PRIMARY)
    strong = _ret(reg_grp({"STRONG_BULL", "BULL"}), PRIMARY)
    weak = _ret(reg_grp({"WEAK_BEAR", "STRONG_BEAR"}), PRIMARY)

    def verdict(diff, t):
        if diff is None:
            return "no data"
        sig = t is not None and abs(t) >= 1.96
        if diff > 0:
            return "YES (significant)" if sig else "yes, not significant"
        return "INVERTED (significant)" if sig else "no"

    def qrow(label, top, bot, top_lbl, bot_lbl):
        mt, mb = _mean(top), _mean(bot)
        diff = round(mt - mb, 2) if (mt is not None and mb is not None) else None
        t = _welch_t(top, bot)
        return [label, f"{_fmt(mt,'%')} ({len(_clean(top))})", f"{_fmt(mb,'%')} ({len(_clean(bot))})",
                _fmt(diff, "%"), _fmt(t), verdict(diff, t)]

    q_rows = [
        qrow("1. ACTION_NOW outperforms universe", an20, _ret(records, PRIMARY), "ACTION_NOW", "all"),
        qrow("2. WATCHLIST outperforms universe", wl20, _ret(records, PRIMARY), "WATCHLIST", "all"),
        qrow("3. EXTENDED underperforms universe", ex20, _ret(records, PRIMARY), "EXTENDED", "all"),
        qrow("4. AVOID < ACTION_NOW (capital protect)", av20, an20, "AVOID", "ACTION_NOW"),
        qrow("7. MARKET_LEADER > SECTOR_LEADER", ml20, sl20, "MKT_LEADER", "SEC_LEADER"),
        qrow("8. A+ > A", ap20, a20, "A+", "A"),
        qrow("9. Strong regimes > weak regimes", strong, weak, "STRONG/BULL", "WEAK/STRONG_BEAR"),
    ]

    # ── failure cases: worst ACTION_NOW observations (20D) ───────────────────
    an_recs = [r for r in grp("ACTION_NOW") if r.fwd.get(PRIMARY) is not None]
    worst = sorted(an_recs, key=lambda r: r.fwd.get(PRIMARY))[:12]
    fail_rows = [[r.screen_date, r.ticker.replace(".NS", ""), r.regime,
                  f"{r.composite_score:.0f}", _fmt(r.fwd.get(PRIMARY), "%"),
                  _fmt(r.fwd.get('mae20'), "%")] for r in worst]

    # ── assemble markdown ────────────────────────────────────────────────────
    mono_ok = _mono([_mean(_ret(by_dec.get(d, []), PRIMARY)) for d in
                     ["90-100", "80-89", "70-79", "60-69", "50-59"]])
    L = [f"# ACTION_NOW — 5-Year Forensic Validation", "",
         f"_Generated {datetime.now().isoformat(timespec='seconds')} · "
         f"{n_dates} weekly screens · {n} observations · {drange}_", "",
         "> Measurement only. The screener was replayed exactly as it exists today "
         "(production Phase 2–7 engines, point-in-time). No scoring, threshold, regime, "
         "ranking or factor was changed. Primary horizon = 20 trading days.", "",
         "## 1. Executive Summary", "",
         f"- Universe forward return (20D, all obs): **{_fmt(uni20,'%')}** (n={len(_clean(_ret(records,PRIMARY)))}).",
         f"- ACTION_NOW 20D avg **{_fmt(_mean(an20),'%')}** (win {_fmt(_win(an20))}, n={len(_clean(an20))}) "
         f"vs AVOID **{_fmt(_mean(av20),'%')}**.",
         f"- Composite→return rank correlation: **{_fmt(comp_corr)}** · Actionability→return: **{_fmt(act_corr)}**.",
         f"- Score-decile monotonicity (higher decile ≥ next): **{'HOLDS' if mono_ok else 'BROKEN — flagged'}**.",
         "", "## 2. Key Findings (the 9 questions)", "",
         _tbl(["Question", "Top", "Bottom", "Diff", "Welch t", "Verdict"], q_rows),
         "", "_Q5 (composite→returns) and Q6 (actionability→returns) are answered by §4 "
         "and the correlations above. ‘Significant’ = |t| ≥ 1.96._", "",
         "## 3. Classification Performance (20D)", "",
         _tbl(["Class", "n", "Win", "Avg", "Median", "Avg DD(MAE)", "Worst DD", "Sharpe"], cls_detail),
         "", "### Closing return by horizon", "",
         _tbl(["Class"] + [f"{h}D" for h in HORIZONS], cls_horizon),
         "", "## 4. Score Decile Performance (composite, 20D)", "",
         _tbl(["Decile", "n", "Win", "Avg", "Median", "Avg DD(MAE)"], dec_rows),
         f"\n_Expected: higher decile → higher avg return. Monotonic: "
         f"**{'YES' if mono_ok else 'NO — FLAGGED'}**._", "",
         "### Actionability quintiles (Q6)", "",
         _tbl(["Actionability", "n", "Win", "Avg 20D"], actq_rows),
         "", "## 5. Regime Performance (20D)", "",
         _tbl(["Regime", "n", "Win", "Avg", "Avg DD(MAE)"], reg_rows),
         "", "### ACTION_NOW within each regime", "",
         _tbl(["Regime", "n", "Win", "Avg ACTION_NOW", "Avg DD(MAE)"], regan_rows),
         "", "## 6. Grade Performance (20D)", "",
         _tbl(["Grade", "n", "Win", "Avg", "Median"], grade_rows),
         "", "## 7. RS Performance (20D)", "",
         _tbl(["RS status", "n", "Win", "Avg"], rs_rows),
         "", "## 8. Failure Cases — worst 12 ACTION_NOW outcomes (20D)", "",
         _tbl(["Screen date", "Ticker", "Regime", "Comp", "20D ret", "Max DD"], fail_rows),
         "", "## 9. Recommendations (findings only — system NOT modified)", "",
         _recommendations(an20, av20, ex20, wl20, ml20, sl20, ap20, a20, strong, weak,
                          comp_corr, act_corr, mono_ok, uni20),
         ""]
    return "\n".join(L) + "\n"


def _mono(seq) -> bool:
    s = [x for x in seq if x is not None]
    return all(s[i] >= s[i + 1] for i in range(len(s) - 1)) if len(s) >= 2 else False


def _recommendations(an, av, ex, wl, ml, sl, ap, a, strong, weak,
                     comp_corr, act_corr, mono_ok, uni) -> str:
    recs = []
    man, mav = _mean(an), _mean(av)
    if man is not None and mav is not None:
        if man > mav:
            recs.append(f"- ACTION_NOW ({man}%) beat AVOID ({mav}%) on point estimate — "
                        "consistent with edge; confirm significance/n before sizing up.")
        else:
            recs.append(f"- ⚠ ACTION_NOW ({man}%) did NOT beat AVOID ({mav}%) — investigate before trusting the label.")
    if not mono_ok:
        recs.append("- ⚠ Composite score deciles are NOT monotonic — higher scores did not reliably "
                    "produce higher returns. Treat composite grade as a weak ranking signal (consistent "
                    "with the prior 'grade is fragile' finding). Do NOT tune — measure more.")
    else:
        recs.append("- Composite deciles are monotonic — score ordering tracks forward return.")
    if comp_corr is not None and abs(comp_corr) < 0.05:
        recs.append(f"- Composite→return correlation is ~0 ({comp_corr}); the score's value is in the "
                    "extreme buckets, not as a continuous predictor.")
    mml, msl = _mean(ml), _mean(sl)
    if mml is not None and msl is not None:
        recs.append(f"- MARKET_LEADER ({mml}%) vs SECTOR_LEADER ({msl}%): "
                    + ("leaders-of-leaders premium present." if mml > msl else "no market-leader premium over sector — flag."))
    mstrong, mweak = _mean(strong), _mean(weak)
    if mstrong is not None and mweak is not None:
        recs.append(f"- Strong regimes ({mstrong}%) vs weak ({mweak}%): "
                    + ("regime classification adds value." if mstrong > mweak else "regime ordering INVERTED — flag."))
    recs.append("- Next step is NOT a code change: it is more out-of-sample weeks + significance, then "
                "decide promotions against pre-registered thresholds. No optimization in this phase.")
    return "\n".join(recs)


# ─────────────────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────────────────
def run(years: float = 5.0, every_weeks: int = 1, period: str = "5y",
        sample: int = 0) -> Path:
    from scanner.universe_builder import build_universe
    from scanner.data_feed import get_ohlcv
    from scanner.relative_strength_engine import fetch_nifty

    print(f"  Assembling data (period={period}, sample={sample or 'full'})...", flush=True)
    uni = build_universe()
    tickers = uni.tickers[:sample] if sample else uni.tickers
    sub = uni.df[uni.df["ticker"].isin(tickers)]
    sector_map = dict(zip(sub["ticker"], sub["sector"]))
    feed = get_ohlcv(tickers, period=period, min_bars=60)
    nifty = fetch_nifty(period=period)
    print(f"  Data: {len(feed.data)} tickers, nifty={'OK' if nifty is not None else 'MISSING'}. "
          f"Replaying {years}y weekly (every {every_weeks}w)...", flush=True)

    records, asofs = generate_rolling_records(feed.data, sector_map, nifty, years, every_weeks)
    ForwardReturnEngine().attach(records, feed.data, HORIZONS)   # adds 120D
    _attach_excursions(records, feed.data, window=20)

    dates = sorted({r.screen_date for r in records})
    drange = f"{dates[0]} → {dates[-1]}" if dates else "—"
    md = build_report(records, len(dates), drange)

    _OUT.mkdir(parents=True, exist_ok=True)
    path = _OUT / "action_now_5y.md"
    path.write_text(md, encoding="utf-8")
    print(f"\n  ✓ Wrote {path}  ({len(records)} obs, {len(dates)} screens)\n", flush=True)
    return path


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    yrs = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    evw = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    smp = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    run(years=yrs, every_weeks=evw, sample=smp)
