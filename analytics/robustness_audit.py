"""
analytics/robustness_audit.py — Phase 4 Robustness Audit (MEASUREMENT ONLY)
===========================================================================
Determines whether the edge measured in Phases D/E (5-Year Validation + Edge
Attribution) is ROBUST, PROMISING-BUT-FRAGILE, UNPROVEN or LIKELY-OVERFIT.

It replays the screener EXACTLY as it exists today (the same point-in-time
Phase 2–7 stack used by analytics.edge_attribution), captures one row per
(screen_date, ticker) with component scores + forward returns, and then runs six
robustness lenses:

  1. Edge stability across 5 equal time periods (return / win / drawdown / Sharpe)
  2. Regime stability (BULL / NEUTRAL / BEAR)
  3. Sector dependency (jackknife — remove a sector, does the edge survive?)
  4. Stock dependency (remove top-10 / top-20 contributors)
  5. Factor dependency (equal-weight leave-one-out: essential vs redundant vs passenger)
  6. Failure-cluster analysis (worst 10% outcomes — do failures cluster?)

It changes NO scoring, threshold, regime, ranking or factor. It tunes nothing and
recommends nothing. It only measures and classifies.

Output: reports/validation/robustness_audit.md
Dataset cache (so the slow replay runs once): reports/validation/robustness_dataset.csv

    python -m analytics.robustness_audit [years] [every_weeks] [sample] [--reuse]

`--reuse` loads the cached dataset CSV instead of replaying (for fast report edits).
"""
from __future__ import annotations

import statistics as st
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from analytics.edge_attribution import _replay_components, _attach_forward
from analytics.rolling_validation import _weekly_asofs, _welch_t

_OUT = Path("reports/validation")
_DATASET = _OUT / "robustness_dataset.csv"
PRIMARY = "fwd20"

# macro-regime grouping for Section 3
MACRO = {
    "STRONG_BULL": "BULL", "BULL": "BULL",
    "NEUTRAL": "NEUTRAL",
    "WEAK_BEAR": "BEAR", "STRONG_BEAR": "BEAR",
}
# equal-weight component blend used for factor-dependency leave-one-out
EW_COMPONENTS = ["rs", "sector_c", "trend", "breakout", "liquidity", "atr", "freshness"]
# the proven edge cohort used for dependency/stability lenses (n is large; ACTION_NOW is tiny)
LEADER = "MARKET_LEADER"


# ─────────────────────────────────────────────────────────────────────────────
# SMALL STAT HELPERS (operate on pandas Series / lists; no scipy)
# ─────────────────────────────────────────────────────────────────────────────
def _m(s) -> Optional[float]:
    s = pd.Series(s).dropna()
    return round(float(s.mean()), 2) if len(s) else None


def _win(s) -> Optional[float]:
    s = pd.Series(s).dropna()
    return round(float((s > 0).mean()), 3) if len(s) else None


def _sharpe(s) -> Optional[float]:
    s = pd.Series(s).dropna()
    if len(s) < 2:
        return None
    sd = float(s.std(ddof=0))
    return round(float(s.mean()) / sd, 2) if sd else None


def _fmt(v, suffix="") -> str:
    if v is None or (isinstance(v, float) and v != v):
        return "—"
    return f"{v}{suffix}"


def _tbl(header, rows) -> str:
    L = ["| " + " | ".join(map(str, header)) + " |",
         "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        L.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(L)


def _excess(cohort: pd.DataFrame, uni: pd.DataFrame, col=PRIMARY) -> Optional[float]:
    mc, mu = _m(cohort[col]), _m(uni[col])
    return round(mc - mu, 2) if (mc is not None and mu is not None) else None


def _wt(a, b) -> Optional[float]:
    """Welch t that drops NaN as well as None (the most-recent screens have NaN 20D
    forward returns; the shared _welch_t only filters None, which would poison the t)."""
    return _welch_t(pd.Series(a).dropna().tolist(), pd.Series(b).dropna().tolist())


def _ew_spread(df: pd.DataFrame, cols, ret=PRIMARY, q=5) -> Optional[float]:
    """Top-quintile − bottom-quintile mean forward return for an EQUAL-WEIGHT rank
    blend of `cols` (percentile-rank each column, sum, quintile, top−bottom)."""
    use = [c for c in cols if c in df.columns]
    d = df[use + [ret]].dropna()
    if len(d) < 5 * q or not use:
        return None
    score = sum(d[c].rank(pct=True) for c in use)
    try:
        b = pd.qcut(score.rank(method="first"), q, labels=False)
    except Exception:
        return None
    return round(float(d[b == q - 1][ret].mean() - d[b == 0][ret].mean()), 2)


def _classify_series(vals) -> str:
    """Classify a per-period excess series as stable / improving / deteriorating /
    unstable / negative, from sign-consistency + slope."""
    s = [v for v in vals if v is not None]
    if len(s) < 3:
        return "insufficient"
    pos = sum(1 for v in s if v > 0)
    n = len(s)
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(s) / n
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((xs[i] - mx) * (s[i] - my) for i in range(n)) / denom if denom else 0.0
    rng = max(s) - min(s)
    if pos == n:
        if slope > 0.15 * (abs(my) + 1e-9):
            return "stable (improving)"
        if slope < -0.15 * (abs(my) + 1e-9):
            return "stable (fading, still +)"
        return "stable"
    if pos == 0:
        return "negative throughout"
    # mixed signs
    inc = all(s[i] <= s[i + 1] for i in range(n - 1))
    dec = all(s[i] >= s[i + 1] for i in range(n - 1))
    if inc:
        return "improving"
    if dec:
        return "deteriorating"
    return "unstable"


# ─────────────────────────────────────────────────────────────────────────────
# SECTION BUILDERS
# ─────────────────────────────────────────────────────────────────────────────
def _periods(df: pd.DataFrame, k=5):
    """Split unique sorted screen-dates into k contiguous equal buckets.
    Returns (period_label_series_aligned_to_df, list_of_(label, lo_date, hi_date))."""
    dates = sorted(df["screen_date"].unique())
    chunks = np.array_split(np.array(dates), k)
    label_of = {}
    meta = []
    for i, ch in enumerate(chunks, 1):
        lbl = f"P{i}"
        for d in ch:
            label_of[d] = lbl
        meta.append((lbl, ch[0], ch[-1]))
    return df["screen_date"].map(label_of), meta


def _sec_time_stability(df: pd.DataFrame, uni: pd.DataFrame) -> tuple[str, dict]:
    period, meta = _periods(df, 5)
    n_dates = df["screen_date"].nunique()
    df = df.assign(_p=period)
    uni = uni.assign(_p=period.loc[uni.index])
    an_excess, ld_excess = [], []
    rows_an, rows_ld, rows_uni = [], [], []
    for lbl, lo, hi in meta:
        seg = df[df["_p"] == lbl]
        segu = uni[uni["_p"] == lbl]
        an = seg[seg["classification"] == "ACTION_NOW"]
        ld = seg[seg["rs_status"] == LEADER]
        u_m = _m(segu[PRIMARY])
        rows_uni.append([lbl, f"{lo}→{hi}", len(segu), _fmt(u_m, "%"),
                         _fmt(_win(segu[PRIMARY])), _fmt(_m(segu["mae20"]), "%")])
        ax = _excess(an, segu)
        lx = _excess(ld, segu)
        an_excess.append(ax)
        ld_excess.append(lx)
        rows_an.append([lbl, len(an), _fmt(_m(an[PRIMARY]), "%"), _fmt(_win(an[PRIMARY])),
                        _fmt(_m(an["mae20"]), "%"), _fmt(_sharpe(an[PRIMARY])), _fmt(ax, "%"),
                        _fmt(_wt(an[PRIMARY], segu[PRIMARY]))])
        rows_ld.append([lbl, len(ld), _fmt(_m(ld[PRIMARY]), "%"), _fmt(_win(ld[PRIMARY])),
                        _fmt(_m(ld["mae20"]), "%"), _fmt(_sharpe(ld[PRIMARY])), _fmt(lx, "%"),
                        _fmt(_wt(ld[PRIMARY], segu[PRIMARY]))])
    an_cls = _classify_series(an_excess)
    ld_cls = _classify_series(ld_excess)
    # concentration: share of total positive leader-excess from the single best period
    pos = [x for x in ld_excess if x and x > 0]
    conc = round(max(pos) / sum(pos), 2) if pos else None

    body = [
        f"Each period is an equal slice of the {n_dates} weekly screens. *Excess* = cohort mean − "
        "that period's universe mean (20D). The proven RS-leadership cohort (MARKET_LEADER, "
        "large n) is the reliable stability gauge; ACTION_NOW is shown but per-period n is tiny.",
        "",
        "**Universe baseline by period**", "",
        _tbl(["Period", "Dates", "n", "Uni 20D", "Win", "Avg DD"], rows_uni), "",
        "**MARKET_LEADER cohort by period** (proven edge)", "",
        _tbl(["Period", "n", "Avg 20D", "Win", "Avg DD", "Sharpe", "Excess", "t vs uni"], rows_ld), "",
        "**ACTION_NOW by period** (small n — directional only)", "",
        _tbl(["Period", "n", "Avg 20D", "Win", "Avg DD", "Sharpe", "Excess", "t vs uni"], rows_an), "",
        f"- Leader-edge classification across periods: **{ld_cls}** "
        f"(excess by period: {['%+.2f' % x if x is not None else None for x in ld_excess]}).",
        f"- ACTION_NOW-edge classification across periods: **{an_cls}** "
        f"(excess by period: {['%+.2f' % x if x is not None else None for x in an_excess]}).",
        f"- Concentration: the single strongest period supplies **{_fmt(conc)}** of all positive "
        "leader-excess (1.0 = entirely one period; ~0.2 = evenly spread across 5).",
    ]
    return "\n".join(body), {"leader_excess": ld_excess, "an_excess": an_excess,
                             "leader_cls": ld_cls, "an_cls": an_cls, "concentration": conc}


def _sec_regime_stability(df: pd.DataFrame) -> tuple[str, dict]:
    df = df.assign(_macro=df["regime"].map(MACRO))
    rows, verdict = [], {}
    order = ["BULL", "NEUTRAL", "BEAR"]
    for mr in order:
        seg = df[df["_macro"] == mr]
        if seg.empty:
            continue
        uni = seg  # within-regime universe
        ld = seg[seg["rs_status"] == LEADER]
        lag = seg[seg["rs_status"] == "LAGGARD"]
        an = seg[seg["classification"] == "ACTION_NOW"]
        lx = _excess(ld, uni)                 # leader vs within-regime universe (context)
        ml, mlag = _m(ld[PRIMARY]), _m(lag[PRIMARY])
        diff = round(ml - mlag, 2) if (ml is not None and mlag is not None) else None
        t_ll = _wt(ld[PRIMARY], lag[PRIMARY])  # canonical RS contrast: leader vs laggard
        sig = t_ll is not None and abs(t_ll) >= 1.96
        # verdict on the proven RS edge (MARKET_LEADER vs LAGGARD) inside this regime
        if diff is None:
            v = "n/a"
        elif diff > 0 and sig:
            v = "edge holds (sig)"
        elif diff > 0:
            v = "edge weak (+, not sig)"
        elif diff < 0 and sig:
            v = "edge REVERSES (sig)"
        else:
            v = "edge disappears"
        verdict[mr] = (diff, t_ll, v)
        rows.append([mr, len(seg), _fmt(_m(seg[PRIMARY]), "%"), _fmt(_win(seg[PRIMARY])),
                     _fmt(_m(seg["mae20"]), "%"),
                     _fmt(ml, "%"), _fmt(mlag, "%"), _fmt(lx, "%"),
                     _fmt(diff, "%"), _fmt(t_ll),
                     f"{_fmt(_m(an[PRIMARY]),'%')} (n={len(an.dropna(subset=[PRIMARY]))})", v])
    body = [
        "Regimes grouped: **BULL** = STRONG_BULL+BULL · **NEUTRAL** = NEUTRAL · "
        "**BEAR** = WEAK_BEAR+STRONG_BEAR. *Ldr excess* = MARKET_LEADER − within-regime "
        "universe (context). The verdict uses the canonical RS contrast **Leader − Laggard** "
        "with its Welch t inside each regime.", "",
        _tbl(["Regime", "n", "Uni 20D", "Win", "Avg DD", "Leader", "Laggard",
              "Ldr excess", "L−Lag", "t (L vs Lag)", "ACTION_NOW", "Edge verdict"], rows),
    ]
    return "\n".join(body), verdict


def _sec_sector_dependency(df: pd.DataFrame, uni: pd.DataFrame) -> tuple[str, dict]:
    ld = df[df["rs_status"] == LEADER]
    base_excess = _excess(ld, uni)
    sectors = (ld.groupby("sector")[PRIMARY]
                 .agg(["count", "mean"]).sort_values("count", ascending=False))
    rows = []
    degr = {}
    for sec, r in sectors.iterrows():
        seg_ld = ld[ld["sector"] == sec]
        seg_u = uni[uni["sector"] == sec]
        within = _excess(seg_ld, seg_u)
        # jackknife: remove this sector from BOTH leader cohort and universe baseline
        ld_excl = ld[ld["sector"] != sec]
        uni_excl = uni[uni["sector"] != sec]
        excl_excess = _excess(ld_excl, uni_excl)
        drop = round(base_excess - excl_excess, 2) if (base_excess is not None and excl_excess is not None) else None
        degr[sec] = (int(r["count"]), excl_excess, drop)
        rows.append([sec, int(r["count"]), _fmt(round(r["mean"], 2), "%"),
                     _fmt(within, "%"), _fmt(excl_excess, "%"), _fmt(drop, "%")])
    rows.sort(key=lambda x: -(x[1]))
    # strongest sector = the one whose removal degrades the leader edge most
    strongest = max(degr.items(), key=lambda kv: (kv[1][2] if kv[1][2] is not None else -99))
    s_name, (s_n, s_excl, s_drop) = strongest
    survive_t = None
    if s_excl is not None:
        ld_excl = ld[ld["sector"] != s_name]
        uni_excl = uni[uni["sector"] != s_name]
        survive_t = _wt(ld_excl[PRIMARY], uni_excl[PRIMARY])
    body = [
        f"Edge metric = MARKET_LEADER excess over universe (base = **{_fmt(base_excess,'%')}**, "
        f"n={len(ld.dropna(subset=[PRIMARY]))} leader-obs). *Excl-sector excess* = jackknife "
        "recomputing BOTH leader cohort and universe with that sector removed.", "",
        _tbl(["Sector", "Ldr n", "Ldr mean", "Within-sec excess", "Excess if removed", "Degradation"], rows), "",
        f"- Strongest single-sector dependency: **{s_name}** (removing it changes leader-excess by "
        f"**{_fmt(s_drop,'%')}** → residual excess **{_fmt(s_excl,'%')}**, "
        f"t={_fmt(survive_t)}).",
        f"- Edge after removing the strongest sector: "
        + ("**survives** (still positive)." if (s_excl is not None and s_excl > 0)
           else "**does not survive** (collapses to ≤0)." if s_excl is not None else "n/a"),
    ]
    return "\n".join(body), {"base_excess": base_excess, "strongest": s_name,
                             "excl_excess": s_excl, "drop": s_drop, "survive_t": survive_t}


def _stock_dep_for(cohort: pd.DataFrame, uni: pd.DataFrame, label: str, tops=(10, 20)):
    u20 = _m(uni[PRIMARY])
    base_m = _m(cohort[PRIMARY])
    base_x = _excess(cohort, uni)
    work = cohort.dropna(subset=[PRIMARY]).copy()
    work["_xs"] = work[PRIMARY] - (u20 or 0.0)
    contrib = work.groupby("ticker")["_xs"].sum().sort_values(ascending=False)
    total_pos = contrib[contrib > 0].sum()
    n_names = contrib.shape[0]
    rows, out = [], {}
    rows.append([label + " (base)", len(work), _fmt(base_m, "%"), _fmt(base_x, "%"), "—", "—"])
    for k in tops:
        top_names = set(contrib.head(k).index)
        share = round(float(contrib.head(k).sum() / total_pos), 2) if total_pos else None
        rest = work[~work["ticker"].isin(top_names)]
        rm_m = _m(rest[PRIMARY])
        rm_x = round(rm_m - (u20 or 0.0), 2) if rm_m is not None else None
        t = _wt(rest[PRIMARY], uni[PRIMARY])
        rows.append([f"− top {k} of {n_names}", len(rest), _fmt(rm_m, "%"),
                     _fmt(rm_x, "%"), _fmt(share), _fmt(t)])
        out[k] = (rm_x, share, t)
    top_list = ", ".join(f"{t.replace('.NS','')} ({v:+.0f})"
                         for t, v in contrib.head(10).items())
    return rows, out, top_list, n_names


def _sec_stock_dependency(df: pd.DataFrame, uni: pd.DataFrame) -> tuple[str, dict]:
    ld = df[df["rs_status"] == LEADER]
    an = df[df["classification"] == "ACTION_NOW"]
    ld_rows, ld_out, ld_top, ld_names = _stock_dep_for(ld, uni, "MARKET_LEADER")
    an_rows, an_out, an_top, an_names = _stock_dep_for(an, uni, "ACTION_NOW", tops=(10, 20))
    body = [
        "Contribution per name = Σ(obs 20D return − universe mean). Removing the biggest "
        "contributors and recomputing tests whether the edge is broad-based or carried by a "
        "few names. *Share* = fraction of all positive excess supplied by those names.", "",
        f"**MARKET_LEADER cohort** ({ld_names} distinct names)", "",
        _tbl(["Cut", "n", "Avg 20D", "Excess", "Share of +excess", "t vs uni"], ld_rows), "",
        f"Top-10 leader contributors: {ld_top}.", "",
        f"**ACTION_NOW cohort** ({an_names} distinct names — small)", "",
        _tbl(["Cut", "n", "Avg 20D", "Excess", "Share of +excess", "t vs uni"], an_rows), "",
        f"Top-10 ACTION_NOW contributors: {an_top}.",
    ]
    return "\n".join(body), {"leader": ld_out, "action_now": an_out,
                             "leader_names": ld_names, "an_names": an_names}


def _sec_factor_dependency(df: pd.DataFrame) -> tuple[str, dict]:
    base = _ew_spread(df, EW_COMPONENTS)
    comp_spread = _ew_spread(df, ["composite"])  # production blend as a single ranked column
    rows = []
    loo = {}
    for f in ["rs", "sector_c", "trend", "breakout", "atr", "liquidity", "freshness"]:
        sub = [c for c in EW_COMPONENTS if c != f]
        sp = _ew_spread(df, sub)
        delta = round(base - sp, 2) if (base is not None and sp is not None) else None
        if delta is None:
            verdict = "—"
        elif delta >= 0.4:
            verdict = "ESSENTIAL (removing hurts)"
        elif delta <= -0.4:
            verdict = "PASSENGER (removing helps)"
        else:
            verdict = "redundant / neutral"
        loo[f] = (sp, delta, verdict)
        rows.append([f, _fmt(sp, "%"), _fmt(delta, "%"), verdict])
    rows.sort(key=lambda r: -(float(r[2].rstrip("%")) if r[2] != "—" else -99))
    body = [
        "Equal-weight rank blend of all 7 components → Q5−Q1 spread = "
        f"**{_fmt(base,'%')}** (baseline). Production composite (as one ranked column) spread = "
        f"**{_fmt(comp_spread,'%')}**. Leave-one-out removes each factor and recomputes; "
        "Δ = baseline − LOO (positive Δ ⇒ the factor was *carrying* edge; negative Δ ⇒ it was "
        "*diluting* it).", "",
        _tbl(["Removed factor", "Spread without it", "Δ vs baseline", "Verdict"], rows),
    ]
    return "\n".join(body), {"base": base, "composite": comp_spread, "loo": loo}


def _profile(seg: pd.DataFrame, cols) -> dict:
    return {c: (round(float(seg[c].mean()), 0) if seg[c].notna().any() else None) for c in cols}


def _sec_failure_clusters(df: pd.DataFrame) -> tuple[str, dict]:
    prof_cols = ["rs", "composite", "actionability", "trend", "breakout", "atr", "liquidity"]
    out = {}
    parts = ["Worst-decile = the bottom 10% of a cohort's 20D outcomes. We compare the failure "
             "slice's regime mix, sector mix, drawdown and factor profile against the cohort "
             "average to see whether losers share conditions.", ""]
    for label, cohort in [("ACTION_NOW", df[df["classification"] == "ACTION_NOW"]),
                          ("MARKET_LEADER", df[df["rs_status"] == LEADER])]:
        c = cohort.dropna(subset=[PRIMARY])
        if len(c) < 10:
            continue
        thresh = c[PRIMARY].quantile(0.10)
        worst = c[c[PRIMARY] <= thresh]
        macro = worst["regime"].map(MACRO).value_counts(normalize=True).round(2).to_dict()
        base_macro = c["regime"].map(MACRO).value_counts(normalize=True).round(2).to_dict()
        sec = worst["sector"].value_counts().head(4).to_dict()
        wp = _profile(worst, prof_cols)
        cp = _profile(c, prof_cols)
        out[label] = {"n_worst": len(worst), "thresh": round(float(thresh), 2),
                      "macro": macro, "base_macro": base_macro, "sectors": sec,
                      "worst_profile": wp, "cohort_profile": cp,
                      "worst_mae": _m(worst["mae20"]), "cohort_mae": _m(c["mae20"])}
        prof_rows = [[k, _fmt(cp[k]), _fmt(wp[k])] for k in prof_cols]
        parts += [
            f"**{label}** — worst {len(worst)} of {len(c)} (≤ {round(float(thresh),2)}% 20D)", "",
            f"- Regime mix (worst vs cohort): " +
            " · ".join(f"{k} {worst['regime'].map(MACRO).eq(k).mean():.0%}/"
                       f"{c['regime'].map(MACRO).eq(k).mean():.0%}" for k in ["BULL", "NEUTRAL", "BEAR"]),
            f"- Worst-slice drawdown (MAE) {_fmt(out[label]['worst_mae'],'%')} vs cohort "
            f"{_fmt(out[label]['cohort_mae'],'%')}.",
            f"- Top sectors among failures: " + ", ".join(f"{k} ({v})" for k, v in sec.items()) + ".",
            "",
            _tbl(["Factor (mean)", "Cohort", "Worst decile"], prof_rows), "",
        ]
    return "\n".join(parts), out


# ─────────────────────────────────────────────────────────────────────────────
# TRUST ASSESSMENT
# ─────────────────────────────────────────────────────────────────────────────
def _trust(meta, an_n: int) -> tuple[str, str]:
    t, reg, sec, stk, fac = (meta["time"], meta["regime"], meta["sector"],
                             meta["stock"], meta["factor"])
    lines = []

    # 1. time
    lines.append(f"- **Time stability:** RS-leadership edge is *{t['leader_cls']}* across 5 "
                 f"periods; strongest period supplies {_fmt(t['concentration'])} of positive excess. "
                 f"ACTION_NOW edge is *{t['an_cls']}* (n≈{max(an_n // 5, 1)}/period — not decisive).")
    # 2. regime
    holds = sum(1 for v in reg.values() if v[2].startswith("edge holds"))
    rev = sum(1 for v in reg.values() if "REVERSES" in v[2])
    lines.append(f"- **Regime stability:** leader edge holds significantly in {holds}/3 macro-regimes; "
                 f"reverses in {rev}. " +
                 "; ".join(f"{k}: {v[2]} ({_fmt(v[0],'%')})" for k, v in reg.items()) + ".")
    # 3. sector
    survives_sec = sec["excl_excess"] is not None and sec["excl_excess"] > 0
    lines.append(f"- **Sector dependency:** removing the strongest sector ({sec['strongest']}) moves "
                 f"the leader edge by {_fmt(sec['drop'],'%')} to {_fmt(sec['excl_excess'],'%')} — "
                 + ("edge survives." if survives_sec else "edge does NOT survive."))
    # 4. stock
    ld10 = stk["leader"].get(10, (None, None, None))
    lines.append(f"- **Stock dependency:** removing the top-10 leader contributors (of "
                 f"{stk['leader_names']}) leaves excess {_fmt(ld10[0],'%')} (top-10 supply "
                 f"{_fmt(ld10[1])} of positive excess) — "
                 + ("broad-based." if (ld10[1] is not None and ld10[1] < 0.5) else "concentrated."))
    # 5. factor
    ess = [f for f, v in fac["loo"].items() if v[2].startswith("ESSENTIAL")]
    pas = [f for f, v in fac["loo"].items() if v[2].startswith("PASSENGER")]
    lines.append(f"- **Factor dependency:** essential = {ess or 'none clearly'} ; "
                 f"passengers/dilutive = {pas or 'none'} ; equal-weight blend spread "
                 f"{_fmt(fac['base'],'%')}.")

    # ── classification logic (evidence-driven) ──────────────────────────────
    leader_pos_periods = sum(1 for x in t["leader_excess"] if x is not None and x > 0)
    stable_time = t["leader_cls"].startswith("stable") or t["leader_cls"] == "improving"
    broad_stock = ld10[1] is not None and ld10[1] < 0.5
    broad_sector = survives_sec
    regime_ok = holds >= 1 and rev == 0

    score = sum([stable_time, broad_stock, broad_sector, regime_ok,
                 leader_pos_periods >= 4])
    if score >= 4 and stable_time and broad_sector and broad_stock:
        cls = "A) Robust"
    elif score >= 2:
        cls = "B) Promising but fragile"
    elif score >= 1:
        cls = "C) Unproven"
    else:
        cls = "D) Likely overfit"

    verdict = [
        f"**Final classification: {cls}**", "",
        f"Robustness checks passed: **{score}/5** "
        f"(stable-in-time={stable_time}, broad-across-stocks={broad_stock}, "
        f"survives-strongest-sector={broad_sector}, regime-consistent={regime_ok}, "
        f"positive-in-≥4/5-periods={leader_pos_periods >= 4}).", "",
        "Scope of the verdict:",
        f"- The verdict is about the **RS-leadership / composite-extreme edge** (the only "
        f"statistically supported signal). ACTION_NOW remains a separate, under-powered question "
        f"(n={an_n}; ~{max(an_n // 5, 1)}/period) and is **not** elevated by this audit.",
        "- ATR's large standalone spread is treated as a **volatility/beta exposure** caveat, not "
        "skill (it inverts in WEAK_BEAR per Phase E) — see Factor Dependency.",
    ]
    return "\n".join(lines), "\n".join(verdict)


# ─────────────────────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────────────────────
def build_report(df: pd.DataFrame, n_dates: int, drange: str) -> str:
    uni = df.dropna(subset=[PRIMARY])
    s_time, m_time = _sec_time_stability(df, uni)
    s_reg, m_reg = _sec_regime_stability(df)
    s_sec, m_sec = _sec_sector_dependency(df, uni)
    s_stk, m_stk = _sec_stock_dependency(df, uni)
    s_fac, m_fac = _sec_factor_dependency(df)
    s_fail, m_fail = _sec_failure_clusters(df)
    uni20 = _m(uni[PRIMARY])
    ld = df[df["rs_status"] == LEADER]
    an = df[df["classification"] == "ACTION_NOW"]
    an_n = len(an.dropna(subset=[PRIMARY]))
    trust_bullets, verdict = _trust({"time": m_time, "regime": m_reg, "sector": m_sec,
                                     "stock": m_stk, "factor": m_fac}, an_n)

    L = [
        "# Robustness Audit — Phase 4", "",
        f"_Generated {datetime.now().isoformat(timespec='seconds')} · {n_dates} weekly screens · "
        f"{len(df)} observations · {drange}_", "",
        "> Measurement only. The screener was replayed exactly as it exists today (production "
        "Phase 2–7 engines, point-in-time, identical to the 5-Year Validation and Edge "
        "Attribution runs). No scoring, threshold, regime, ranking or factor was changed. This "
        "audit *measures and classifies* the durability of the edge; it proposes no changes. "
        "Primary horizon = 20 trading days.", "",
        "## 1. Executive Summary", "",
        f"- Universe 20D mean: **{_fmt(uni20,'%')}** (n={len(uni)}). MARKET_LEADER "
        f"**{_fmt(_m(ld[PRIMARY]),'%')}** (n={len(ld.dropna(subset=[PRIMARY]))}); ACTION_NOW "
        f"**{_fmt(_m(an[PRIMARY]),'%')}** (n={len(an.dropna(subset=[PRIMARY]))}).",
        "- The audit tests the **RS-leadership / composite-extreme** edge (the proven signal) "
        "for durability across time, regime, sector, stock and factor cuts, plus failure "
        f"clustering. ACTION_NOW is reported throughout but is too small (n={an_n}) to classify on its own.",
        trust_bullets, "",
        verdict, "",
        "## 2. Edge Stability (time)", "", s_time, "",
        "## 3. Regime Stability", "", s_reg, "",
        "## 4. Sector Dependency", "", s_sec, "",
        "## 5. Stock Dependency", "", s_stk, "",
        "## 6. Factor Dependency", "", s_fac, "",
        "## 7. Failure Clusters", "", s_fail, "",
        "## 8. Trust Assessment", "", trust_bullets, "", verdict, "",
    ]
    return "\n".join(L) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────────────────
def _build_dataset(years: float, every_weeks: int, period: str, sample: int) -> pd.DataFrame:
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

    cal = (nifty.index if (nifty is not None and len(nifty) > 250)
           else max(feed.data.values(), key=len).index)
    asofs = _weekly_asofs(cal, years, every_weeks)
    print(f"  Data: {len(feed.data)} tickers, nifty={'OK' if nifty is not None else 'MISSING'}. "
          f"Replaying {len(asofs)} weekly screens (capturing components)...", flush=True)

    rows: list[dict] = []
    for i, ts in enumerate(asofs, 1):
        rows += _replay_components(feed.data, sector_map, nifty, ts)
        if i % 10 == 0 or i == len(asofs):
            print(f"    …replayed {i}/{len(asofs)}", flush=True)
    _attach_forward(rows, feed.data)
    df = pd.DataFrame(rows)
    _OUT.mkdir(parents=True, exist_ok=True)
    df.to_csv(_DATASET, index=False)
    print(f"  Cached dataset → {_DATASET} ({len(df)} obs)", flush=True)
    return df


def run(years: float = 5.0, every_weeks: int = 1, period: str = "5y",
        sample: int = 0, reuse: bool = False) -> Path:
    if reuse and _DATASET.exists():
        print(f"  Reusing cached dataset {_DATASET}", flush=True)
        df = pd.read_csv(_DATASET)
    else:
        df = _build_dataset(years, every_weeks, period, sample)

    dates = sorted(df["screen_date"].unique())
    drange = f"{dates[0]} → {dates[-1]}" if len(dates) else "—"
    md = build_report(df, len(dates), drange)

    _OUT.mkdir(parents=True, exist_ok=True)
    path = _OUT / "robustness_audit.md"
    path.write_text(md, encoding="utf-8")
    print(f"\n  ✓ Wrote {path}  ({len(df)} obs, {len(dates)} screens)\n", flush=True)
    return path


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    reuse = "--reuse" in sys.argv[1:]
    yrs = float(args[0]) if len(args) > 0 else 5.0
    evw = int(args[1]) if len(args) > 1 else 1
    smp = int(args[2]) if len(args) > 2 else 0
    run(years=yrs, every_weeks=evw, sample=smp, reuse=reuse)
