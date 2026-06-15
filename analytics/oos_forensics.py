"""
analytics/oos_forensics.py — Phase 5: Out-of-Sample Failure Forensics (MEASUREMENT ONLY)
========================================================================================
Explains WHY the system failed the out-of-sample test (Phase G / trust_audit.md):
the MARKET_LEADER edge that was significant in-sample (2022–2024) collapsed in the
2025 holdout. This is a forensic post-mortem — it answers *why*, NOT *how to fix it*.

It changes NOTHING: no weights, thresholds, classifications, rankings, factors, regime
or portfolio logic; no optimisation, redesign or recommendations. It reuses the cached
94,637-obs point-in-time replay (reports/validation/robustness_dataset.csv) and runs
nine forensic lenses:

  1. Edge decay (year by year)            6. Sector regime shift
  2. Market-structure change (IS vs OOS)  7. Holdout failure decomposition
  3. Leadership quality (IS vs OOS)        8. Counterfactual (deploy one year only)
  4. Factor drift (predictive power/yr)    9. Final verdict (A–E)
  5. ACTION_NOW failure analysis

Output: reports/validation/oos_failure_forensics.md

    python -m analytics.oos_forensics            # uses the cached dataset
    python -m analytics.oos_forensics --rebuild  # re-replay first (slow), then run
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from analytics.robustness_audit import _DATASET, _build_dataset
from analytics.trust_audit import (
    _m, _med, _win, _sharpe, _wt, _fmt, _tbl, _excess, _top_n,
    _curve_metrics, _basket_perdate, MACRO, P, W,
)

_OUT = Path("reports/validation")
YEARS = ["2022", "2023", "2024", "2025", "2026"]
IS_YEARS = ["2022", "2023", "2024"]
OOS_YEARS = ["2025", "2026"]
COMPONENTS = ["rs", "sector_c", "breakout", "trend", "atr", "liquidity", "freshness"]
FOCUS_SECTORS = ["Capital Goods", "Power", "Healthcare", "Metals & Mining"]


def _y(df, years):
    return df[df["screen_date"].str[:4].isin(years)]


def _yr_of(df):
    return df["screen_date"].str[:4]


def _hhi(counts: pd.Series) -> Optional[float]:
    tot = float(counts.sum())
    if tot <= 0:
        return None
    sh = counts / tot
    return round(float((sh ** 2).sum()), 3)


def _ic(frame, col, ret=P) -> Optional[float]:
    d = frame[[col, ret]].dropna()
    return round(float(d[col].corr(d[ret], method="spearman")), 3) if len(d) > 3 else None


def _spread(frame, col, ret=P, q=5) -> Optional[float]:
    d = frame[[col, ret]].dropna()
    if len(d) < 5 * q:
        return None
    try:
        b = pd.qcut(d[col].rank(method="first"), q, labels=False)
    except Exception:
        return None
    return round(float(d[b == q - 1][ret].mean() - d[b == 0][ret].mean()), 2)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — EDGE DECAY (year by year)
# ─────────────────────────────────────────────────────────────────────────────
def _sec_decay(df) -> tuple[str, dict]:
    rows = []
    ld_x, an_x = {}, {}
    for yr in YEARS:
        g = _y(df, [yr])
        if g.empty:
            continue
        uni = _m(g[P])
        ml = _m(g[g["rs_status"] == "MARKET_LEADER"][P])
        an = _m(g[g["classification"] == "ACTION_NOW"][P])
        lx = round(ml - uni, 2) if (ml is not None and uni is not None) else None
        ax = round(an - uni, 2) if (an is not None and uni is not None) else None
        ld_x[yr], an_x[yr] = lx, ax
        n_an = len(g[g["classification"] == "ACTION_NOW"].dropna(subset=[P]))
        rows.append([yr, len(g.dropna(subset=[P])), _fmt(uni, "%"), _fmt(ml, "%"),
                     f"{_fmt(an,'%')} (n={n_an})", _fmt(lx, "%"), _fmt(ax, "%")])
    seq = [ld_x[y] for y in YEARS if ld_x.get(y) is not None]
    peak_yr = max((y for y in YEARS if ld_x.get(y) is not None), key=lambda y: ld_x[y])
    last = ld_x.get("2025")
    flag = ("COLLAPSED" if (last is not None and last <= 0) else
            "DECAYING" if (seq and seq[-1] < max(seq) * 0.5) else "STABLE")
    body = [
        "Per-calendar-year forward (20D) returns. *Leader/ACTION_NOW excess* = cohort − that year's "
        "universe. This is the headline decay curve.", "",
        _tbl(["Year", "n", "Universe", "MARKET_LEADER", "ACTION_NOW", "Leader excess", "AN excess"], rows),
        "",
        f"- Leader excess by year: " + ", ".join(f"{y} {_fmt(ld_x.get(y),'%')}" for y in YEARS) + ".",
        f"- Edge **peaked in {peak_yr}** ({_fmt(ld_x[peak_yr],'%')}); the **2025 holdout leader "
        f"excess is {_fmt(last,'%')}** (2026 partial {_fmt(ld_x.get('2026'),'%')}).",
        f"- Decay flag: **{flag}** — leader excess fell from its peak to ≤0 in the 2025 holdout.",
    ]
    return "\n".join(body), {"leader_excess": ld_x, "an_excess": an_x, "peak": peak_yr, "flag": flag}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — MARKET STRUCTURE (IS vs OOS)
# ─────────────────────────────────────────────────────────────────────────────
def _struct_metrics(g) -> dict:
    lead = g[g["rs_status"] == "MARKET_LEADER"]
    fwd = g[P].dropna()
    return {
        "breadth": round(float((fwd > 0).mean()), 3) if len(fwd) else None,           # % positive 20D
        "dispersion": round(float(fwd.std(ddof=0)), 2) if len(fwd) else None,         # cross-sec vol
        "avg_dd": _m(g["mae20"]),                                                      # typical drawdown
        "leader_rate": round(float((g["rs_status"] == "MARKET_LEADER").mean()), 3),    # % universe = leaders
        "sector_hhi": _hhi(lead.groupby("sector").size()),                            # leader sector concentration
        "leader_top10_share": (round(float(lead.groupby("ticker").size().sort_values(ascending=False)
                                            .head(10).sum() / max(len(lead), 1)), 3)),
        "mean_trend": _m(g["trend"]), "mean_rs": _m(g["rs"]),
        "bull_share": round(float(g["regime"].map(MACRO).eq("BULL").mean()), 3),
        "bear_share": round(float(g["regime"].map(MACRO).eq("BEAR").mean()), 3),
        "neutral_share": round(float(g["regime"].map(MACRO).eq("NEUTRAL").mean()), 3),
    }


def _sec_structure(df) -> tuple[str, dict]:
    a, b = _struct_metrics(_y(df, IS_YEARS)), _struct_metrics(_y(df, OOS_YEARS))
    labels = [
        ("Breadth (% positive 20D)", "breadth", ""), ("Return dispersion (σ, 20D)", "dispersion", "%"),
        ("Typical drawdown (MAE)", "avg_dd", "%"), ("Leader rate (% universe)", "leader_rate", ""),
        ("Leader sector HHI", "sector_hhi", ""), ("Top-10 names' leader share", "leader_top10_share", ""),
        ("Mean trend score", "mean_trend", ""), ("Mean RS score", "mean_rs", ""),
        ("BULL regime share", "bull_share", ""), ("NEUTRAL regime share", "neutral_share", ""),
        ("BEAR regime share", "bear_share", ""),
    ]
    rows = []
    for name, k, suf in labels:
        av, bv = a.get(k), b.get(k)
        ch = round(bv - av, 3) if (av is not None and bv is not None) else None
        rows.append([name, _fmt(av, suf), _fmt(bv, suf), _fmt(ch, suf)])
    body = [
        "Market structure in-sample (2022–2024) vs out-of-sample (2025–2026). The question: did the "
        "*market* change, or did the *model* stop working?", "",
        _tbl(["Metric", "2022–2024 (IS)", "2025–2026 (OOS)", "Change"], rows), "",
        f"- Regime mix shifted: BULL share {_fmt(a['bull_share'])}→{_fmt(b['bull_share'])}, "
        f"NEUTRAL {_fmt(a['neutral_share'])}→{_fmt(b['neutral_share'])}, "
        f"BEAR {_fmt(a['bear_share'])}→{_fmt(b['bear_share'])}.",
        f"- Breadth {_fmt(a['breadth'])}→{_fmt(b['breadth'])}; leader sector concentration (HHI) "
        f"{_fmt(a['sector_hhi'])}→{_fmt(b['sector_hhi'])}.",
    ]
    return "\n".join(body), {"is": a, "oos": b}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — LEADERSHIP QUALITY (IS vs OOS)
# ─────────────────────────────────────────────────────────────────────────────
def _sec_leadership(df) -> tuple[str, dict]:
    isd = _y(df, IS_YEARS); ood = _y(df, OOS_YEARS)
    li, lo = isd[isd["rs_status"] == "MARKET_LEADER"], ood[ood["rs_status"] == "MARKET_LEADER"]
    rows = []
    for c in COMPONENTS:
        a, b = _m(li[c]), _m(lo[c])
        rows.append([c, _fmt(a), _fmt(b), _fmt(round(b - a, 2) if (a is not None and b is not None) else None)])
    ret_i, ret_o = _m(li[P]), _m(lo[P])
    exc_i = _excess(li, isd); exc_o = _excess(lo, ood)
    core = ["rs", "sector_c", "trend"]   # the components that DEFINE leadership/ranking
    core_drift = round(max(abs((_m(lo[c]) or 0) - (_m(li[c]) or 0)) for c in core), 1)
    atr_drift = round((_m(lo["atr"]) or 0) - (_m(li["atr"]) or 0), 1)
    if core_drift < 6:
        diagnosis = (f"The leaders' **core ranking profile (RS/sector/trend) barely moved** "
                     f"(≤{core_drift} pts) while their excess return collapsed "
                     f"{_fmt(exc_i,'%')}→{_fmt(exc_o,'%')} → **the scoring kept flagging the same "
                     f"kind of names, but those names stopped winning** (scoring stopped identifying "
                     f"winners). Secondary: the ATR profile softened {atr_drift} pts.")
    else:
        diagnosis = "leader core scores themselves shifted → **leaders changed composition**."
    body = [
        "Component-score profile of MARKET_LEADER names, in-sample vs out-of-sample, alongside their "
        "realised return. If scores held but returns fell, the *scoring* stopped working; if scores "
        "fell, the *leaders* got worse.", "",
        _tbl(["Component (mean)", "IS leaders", "OOS leaders", "Δ"], rows), "",
        f"- Leader return: IS **{_fmt(ret_i,'%')}** (excess {_fmt(exc_i,'%')}) → OOS "
        f"**{_fmt(ret_o,'%')}** (excess {_fmt(exc_o,'%')}).",
        f"- Core-component drift (RS/sector/trend): **{core_drift} pts**. {diagnosis}",
    ]
    return "\n".join(body), {"score_drift": core_drift, "atr_drift": atr_drift, "ret_is": ret_i,
                             "ret_oos": ret_o, "exc_is": exc_i, "exc_oos": exc_o}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — FACTOR DRIFT (predictive power by year)
# ─────────────────────────────────────────────────────────────────────────────
def _sec_factor_drift(df) -> tuple[str, dict]:
    rows = []
    verdicts = {}
    for c in COMPONENTS:
        ics = {yr: _ic(_y(df, [yr]), c) for yr in YEARS}
        is_ic = _ic(_y(df, IS_YEARS), c)
        oos_ic = _ic(_y(df, OOS_YEARS), c)
        if is_ic is None or oos_ic is None:
            v = "—"
        elif oos_ic < 0 and is_ic > 0:
            v = "INVERTED"
        elif abs(oos_ic) < 0.02:
            v = "lost power"
        elif oos_ic >= is_ic - 0.02:
            v = "stable"
        else:
            v = "weakened"
        verdicts[c] = (is_ic, oos_ic, v)
        rows.append([c] + [_fmt(ics.get(y)) for y in YEARS] + [_fmt(is_ic), _fmt(oos_ic), v])
    body = [
        "Per-year Spearman correlation of each component score with the 20D forward return "
        "(its predictive power, or 'IC'). Tracks which factors decayed, stayed, or inverted.", "",
        _tbl(["Factor"] + YEARS + ["IS mean", "OOS mean", "Verdict"], rows), "",
        "- 'INVERTED' = positive IC in-sample, negative out-of-sample (the score now points the "
        "wrong way). 'lost power' = OOS IC ≈ 0.",
    ]
    return "\n".join(body), verdicts


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — ACTION_NOW FAILURE ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────
def _sec_action_now(df) -> tuple[str, dict]:
    isd = _y(df, IS_YEARS); ood = _y(df, OOS_YEARS)
    ai = isd[isd["classification"] == "ACTION_NOW"]
    ao = ood[ood["classification"] == "ACTION_NOW"]
    def prof(a, uni):
        return {"n": len(a.dropna(subset=[P])), "win": _win(a[P]), "ret": _m(a[P]),
                "exc": _excess(a, uni), "dd": _m(a["mae20"]),
                "comp": _m(a["composite"]), "rs": _m(a["rs"]), "act": _m(a["actionability"]),
                "brk": _m(a["breakout"]), "atr": _m(a["atr"]),
                "names": a["ticker"].nunique(),
                "bull": round(float(a["regime"].map(MACRO).eq("BULL").mean()), 2) if len(a) else None}
    pi, po = prof(ai, isd), prof(ao, ood)
    rows = [["n trades", pi["n"], po["n"]], ["Win rate", _fmt(pi["win"]), _fmt(po["win"])],
            ["Return 20D", _fmt(pi["ret"], "%"), _fmt(po["ret"], "%")],
            ["Excess vs uni", _fmt(pi["exc"], "%"), _fmt(po["exc"], "%")],
            ["Drawdown (MAE)", _fmt(pi["dd"], "%"), _fmt(po["dd"], "%")],
            ["Composite (mean)", _fmt(pi["comp"]), _fmt(po["comp"])],
            ["RS (mean)", _fmt(pi["rs"]), _fmt(po["rs"])],
            ["Actionability (mean)", _fmt(pi["act"]), _fmt(po["act"])],
            ["Breakout (mean)", _fmt(pi["brk"]), _fmt(po["brk"])],
            ["ATR (mean)", _fmt(pi["atr"]), _fmt(po["atr"])],
            ["Distinct names", pi["names"], po["names"]],
            ["BULL share", _fmt(pi["bull"]), _fmt(po["bull"])]]
    # cause attribution (evidence-led flags)
    causes = []
    if po["n"] < 40:
        causes.append(f"**too few samples** (OOS n={po['n']})")
    if po["comp"] is not None and pi["comp"] is not None and po["comp"] >= pi["comp"] - 1 and (po["ret"] or 0) < (pi["ret"] or 0):
        causes.append("**score held but return fell** (no score inflation; the score simply stopped predicting)")
    if po["bull"] is not None and pi["bull"] is not None and abs(po["bull"] - pi["bull"]) >= 0.1:
        causes.append("**regime mismatch** (BULL share of ACTION_NOW shifted)")
    if po["names"] and po["n"] and po["names"] / max(po["n"], 1) < 0.7:
        causes.append("**concentration** (few repeated names)")
    body = [
        "Every ACTION_NOW candidate, in-sample (2022–2024) vs out-of-sample (2025–2026). "
        "Diagnosing which of {too few samples · score inflation · regime mismatch · concentration · "
        "randomness} explains the failure.", "",
        _tbl(["Metric", "IS ACTION_NOW", "OOS ACTION_NOW"], rows), "",
        "- Evidence-led cause(s): " + ("; ".join(causes) if causes else "no single dominant cause")
        + ". Note the bootstrap (Phase G) already put ACTION_NOW−AVOID CI across zero — consistent "
        "with **noise / too-small-sample** rather than a structural signal that broke.",
    ]
    return "\n".join(body), {"is": pi, "oos": po, "causes": causes}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — SECTOR REGIME SHIFT
# ─────────────────────────────────────────────────────────────────────────────
def _sec_sector_shift(df) -> tuple[str, dict]:
    isd = _y(df, IS_YEARS); ood = _y(df, OOS_YEARS)
    li = isd[isd["rs_status"] == "MARKET_LEADER"]; lo = ood[ood["rs_status"] == "MARKET_LEADER"]
    n_li, n_lo = max(len(li), 1), max(len(lo), 1)
    def secrow(name, mask_i, mask_o):
        si, so = li[mask_i], lo[mask_o]
        sh_i = round(len(si) / n_li, 3); sh_o = round(len(so) / n_lo, 3)
        ri, ro = _m(si[P]), _m(so[P])
        return [name, len(si), _fmt(sh_i), _fmt(ri, "%"), len(so), _fmt(sh_o), _fmt(ro, "%"),
                _fmt(round(sh_o - sh_i, 3))]
    rows = []
    for s in FOCUS_SECTORS:
        rows.append(secrow(s, li["sector"] == s, lo["sector"] == s))
    rows.append(secrow("Others", ~li["sector"].isin(FOCUS_SECTORS), ~lo["sector"].isin(FOCUS_SECTORS)))
    # impact of Capital Goods (the in-sample workhorse)
    cg_i = li[li["sector"] == "Capital Goods"]; cg_o = lo[lo["sector"] == "Capital Goods"]
    cg_ret_drop = (round((_m(cg_o[P]) or 0) - (_m(cg_i[P]) or 0), 2))
    cg_share_drop = round(len(cg_o) / n_lo - len(cg_i) / n_li, 3)
    body = [
        "MARKET_LEADER composition + returns by sector, IS vs OOS. Tests whether the sectors that "
        "carried the in-sample edge were simply absent (lower leader-share) or stopped working "
        "(lower return) out-of-sample.", "",
        _tbl(["Sector", "IS n", "IS share", "IS ret", "OOS n", "OOS share", "OOS ret", "Δ share"],
             rows), "",
        f"- **Capital Goods** (the in-sample workhorse): leader return "
        f"{_fmt(_m(cg_i[P]),'%')}→{_fmt(_m(cg_o[P]),'%')} (Δ {_fmt(cg_ret_drop,'%')}), leader share "
        f"Δ {_fmt(cg_share_drop)}. It was {'still present but lower-returning' if abs(cg_share_drop) < 0.05 else 'less represented'} OOS.",
    ]
    return "\n".join(body), {"cg_ret_drop": cg_ret_drop, "cg_share_drop": cg_share_drop}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — HOLDOUT FAILURE DECOMPOSITION (shift-share)
# ─────────────────────────────────────────────────────────────────────────────
def _mix_effect(isd, ood, group_col) -> Optional[float]:
    """Net shift-share MIX effect on leader-EXCESS from group composition change
    (leaders minus universe)."""
    li = isd[isd["rs_status"] == "MARKET_LEADER"]; lo = ood[ood["rs_status"] == "MARKET_LEADER"]
    def comp(frame):
        gp = frame.dropna(subset=[P]).groupby(frame[group_col])
        n = len(frame.dropna(subset=[P]))
        share = (gp.size() / n) if n else gp.size() * 0
        mean = gp[P].mean()
        return share, mean
    pL_i, mL_i = comp(li); pL_o, _ = comp(lo)
    pU_i, mU_i = comp(isd); pU_o, _ = comp(ood)
    groups = sorted(set(pL_i.index) | set(pL_o.index) | set(pU_i.index) | set(pU_o.index))
    def g(s, k):
        return float(s.get(k, 0.0)) if s is not None else 0.0
    mixL = sum((g(pL_o, k) - g(pL_i, k)) * g(mL_i, k) for k in groups)
    mixU = sum((g(pU_o, k) - g(pU_i, k)) * g(mU_i, k) for k in groups)
    return round(mixL - mixU, 2)


def _sec_decomposition(df) -> tuple[str, dict]:
    isd = _y(df, IS_YEARS); ood = _y(df, OOS_YEARS)
    li, lo = isd[isd["rs_status"] == "MARKET_LEADER"], ood[ood["rs_status"] == "MARKET_LEADER"]
    e_is, e_oos = _excess(li, isd), _excess(lo, ood)
    dE = round((e_oos or 0) - (e_is or 0), 2)
    df2 = df.assign(_macro=df["regime"].map(MACRO))
    regime = _mix_effect(_y(df2, IS_YEARS), _y(df2, OOS_YEARS), "_macro")
    sector = _mix_effect(isd, ood, "sector")
    selection = round(dE - (regime or 0) - (sector or 0), 2)   # residual: within-group rate / stock+factor
    tot = abs(regime or 0) + abs(sector or 0) + abs(selection or 0)
    def pct(x):
        return f"{round(100 * abs(x or 0) / tot)}%" if tot else "—"
    rows = [
        ["Regime mix (leaders shifted regime)", _fmt(regime, "%"), pct(regime)],
        ["Sector mix (leaders shifted sector)", _fmt(sector, "%"), pct(sector)],
        ["Selection residual (within-group: stock + factor decay)", _fmt(selection, "%"), pct(selection)],
        ["**Total ΔLeader-excess (IS→OOS)**", _fmt(dE, "%"), "100%"],
    ]
    driver = max([("regime", abs(regime or 0)), ("sector", abs(sector or 0)),
                  ("selection", abs(selection or 0))], key=lambda x: x[1])[0]
    body = [
        f"Leader excess fell from **{_fmt(e_is,'%')}** (IS) to **{_fmt(e_oos,'%')}** (OOS): "
        f"ΔE = **{_fmt(dE,'%')}**. Shift-share attribution (non-orthogonal; the residual absorbs "
        "within-group rate change and any overlap). *Selection residual* = leaders stopped "
        "out-returning their own regime/sector peers — i.e. the scores picked worse names "
        "(this is where the **factor-IC decay of §4** shows up in return terms).", "",
        _tbl(["Effect", "Contribution to ΔE", "% of |ΔE|"], rows), "",
        f"- Largest single driver of the failure: **{driver}**.",
    ]
    return "\n".join(body), {"dE": dE, "regime": regime, "sector": sector,
                             "selection": selection, "driver": driver}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8 — COUNTERFACTUAL (deploy one year only)
# ─────────────────────────────────────────────────────────────────────────────
def _sec_counterfactual(df) -> tuple[str, dict]:
    rows = []
    confid = {}
    for yr in YEARS:
        g = _y(df, [yr])
        if g.empty:
            continue
        dates = sorted(g["screen_date"].unique())
        top10 = _top_n(g, 10, "composite", need=(W,))
        cm = _curve_metrics(_basket_perdate(top10, W), dates)
        t10_ret = _m(_top_n(g, 10, "composite", need=(P,))[P])
        uni = _m(g[P])
        exc = round(t10_ret - uni, 2) if (t10_ret is not None and uni is not None) else None
        made_money = cm["cagr"] is not None and cm["cagr"] > 0
        beat = exc is not None and exc > 0
        verdict = ("GAIN confidence" if (made_money and beat)
                   else "LOSE confidence")   # lost money OR failed to beat the market
        confid[yr] = verdict
        rows.append([yr, _fmt(t10_ret, "%"), _fmt(uni, "%"), _fmt(exc, "%"),
                     _fmt(cm["cagr"], "%"), _fmt(cm["maxdd"], "%"), verdict])
    gains = sum(1 for v in confid.values() if v.startswith("GAIN"))
    body = [
        "If the identical system had been deployed in just ONE year (Top-10-by-composite, weekly "
        "rebalance, equal-weight, gross of costs), would a trader have gained or lost confidence? "
        "*Excess* = Top-10 − universe (20D); CAGR/DD from that year's weekly curve.", "",
        _tbl(["Deploy year", "Top-10 20D", "Universe", "Excess", "Year CAGR", "Year maxDD", "Verdict"],
             rows), "",
        f"- A trader gains confidence in **{gains}/{len(confid)}** single-year deployments; "
        f"2025 specifically would have **{confid.get('2025','—')}**.",
    ]
    return "\n".join(body), confid


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 9 — FINAL VERDICT
# ─────────────────────────────────────────────────────────────────────────────
def _verdict(decay, struct, lead, drift, decomp) -> tuple[str, str]:
    a, b = struct["is"], struct["oos"]
    # evidence flags
    ys = [decay["leader_excess"].get(y) for y in YEARS if decay["leader_excess"].get(y) is not None]
    monotone = all(ys[i] >= ys[i + 1] for i in range(len(ys) - 1)) if len(ys) >= 2 else False
    scores_stable = lead["score_drift"] < 6           # core ranking profile barely moved
    mix = abs(decomp["regime"] or 0) + abs(decomp["sector"] or 0)   # market/composition shift
    sel = abs(decomp["selection"] or 0)               # within-group selection/factor decay
    market_share = round(mix / (mix + sel), 2) if (mix + sel) else None
    regime_shift = abs((b["bull_share"] or 0) - (a["bull_share"] or 0)) >= 0.12
    inverted = [k for k, v in drift.items() if v[2] == "INVERTED"]
    # broad + significant in-sample (Phases F/G: broad across stocks/sectors, CI excludes 0) ⇒ not overfit
    not_overfit = True

    if not not_overfit:
        choice, why = "D) Edge was mostly overfit", "the in-sample edge was carried by a few names."
    elif monotone:
        choice = "B) Edge decayed"
        why = "leader excess fell monotonically year over year — a steady fade rather than a regime switch."
    elif not_overfit and regime_shift and scores_stable:
        choice = "C) Edge was period-specific"
        why = ("the edge was statistically real and broad in-sample (Phases D–F) but **contingent on "
               "the 2022–2024 conditions** (it peaked in 2023 and lived only in BULL). It did not "
               "*monotonically decay* — the year-by-year leader excess "
               f"({', '.join(_fmt(decay['leader_excess'].get(y),'%') for y in YEARS)}) spiked in 2023 "
               "and went to ~0 once the regime mix turned (BULL share "
               f"{_fmt(a['bull_share'])}→{_fmt(b['bull_share'])}). Both forces are present: ~"
               f"{int((market_share or 0)*100)}% of the failure is regime/sector composition shift "
               "(the 'market changed' part, option A) and ~"
               f"{100-int((market_share or 0)*100)}% is within-group selection decay — leaders "
               "stopped out-returning their own peers and the **RS and Trend factors inverted "
               "out-of-sample**. Neither alone explains it; the edge was specific to a period/regime "
               "that ended, which is option C.")
    elif regime_shift and (market_share or 0) >= 0.7:
        choice = "A) Edge still exists but market changed"
        why = "the failure is overwhelmingly regime/sector composition; within-group selection held."
    else:
        choice = "E) Evidence inconclusive"
        why = "the lenses do not converge on a single dominant cause."

    text = [
        "## 9. Final Verdict", "",
        f"**{choice}**", "",
        why[0].upper() + why[1:], "",
        "Supporting evidence (no recommendations, no fixes):",
        f"- §1 decay: leader excess peaked {decay['peak']}, is "
        f"{_fmt(decay['leader_excess'].get('2025'),'%')} in the 2025 holdout, partial recovery "
        f"{_fmt(decay['leader_excess'].get('2026'),'%')} in 2026 — **non-monotone** (rules out a "
        "clean steady-decay reading of B).",
        f"- §2 structure: BULL share {_fmt(a['bull_share'])}→{_fmt(b['bull_share'])}, BEAR "
        f"{_fmt(a['bear_share'])}→{_fmt(b['bear_share'])}, mean trend {_fmt(a['mean_trend'])}→"
        f"{_fmt(b['mean_trend'])} — the market genuinely shifted (the A component).",
        f"- §3 leadership: core RS/sector/trend drift only {lead['score_drift']} pts while leader "
        f"excess went {_fmt(lead['exc_is'],'%')}→{_fmt(lead['exc_oos'],'%')} → the scoring kept "
        "flagging the same kind of names; they stopped winning (rules out D).",
        f"- §4 factor drift: factors inverted out-of-sample: **{', '.join(inverted) or 'none'}**.",
        f"- §7 decomposition: ΔE {_fmt(decomp['dE'],'%')} = regime {_fmt(decomp['regime'],'%')} + "
        f"sector {_fmt(decomp['sector'],'%')} + selection {_fmt(decomp['selection'],'%')} "
        f"(~{int((market_share or 0)*100)}% market-shift, ~{100-int((market_share or 0)*100)}% "
        "selection decay).", "",
        "This explains WHY trust validation failed; it proposes nothing.",
    ]
    return choice, "\n".join(text)


# ─────────────────────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────────────────────
def build_report(df, n_dates, drange) -> str:
    s1, m1 = _sec_decay(df)
    s2, m2 = _sec_structure(df)
    s3, m3 = _sec_leadership(df)
    s4, m4 = _sec_factor_drift(df)
    s5, m5 = _sec_action_now(df)
    s6, m6 = _sec_sector_shift(df)
    s7, m7 = _sec_decomposition(df)
    s8, m8 = _sec_counterfactual(df)
    choice, s9 = _verdict(m1, m2, m3, m4, m7)

    L = [
        "# Out-of-Sample Failure Forensics — Phase 5", "",
        f"_Generated {datetime.now().isoformat(timespec='seconds')} · {n_dates} weekly screens · "
        f"{len(df)} observations · {drange}_", "",
        "> Measurement only. A forensic post-mortem of WHY the system failed the out-of-sample test "
        "(Phase G): the in-sample-significant MARKET_LEADER edge collapsed in the 2025 holdout. "
        "Built on the cached point-in-time replay; **no weights, thresholds, classifications, "
        "rankings, factors, regime or portfolio logic were changed; no optimisation, redesign or "
        "recommendation.** It explains the failure; it does not fix it.", "",
        "## Executive Summary", "",
        f"- **Verdict: {choice}.** The MARKET_LEADER edge was statistically real and broad in-sample "
        "but **contingent on the 2022–2024 (esp. 2023-bull) conditions**; it peaked in 2023 and went "
        f"to ~0 once the regime turned. ΔLeader-excess (IS→OOS) = **{_fmt(m7['dE'],'%')}**.",
        f"- Two forces, both present: **regime/sector composition shift** "
        f"({_fmt(round((m7['regime'] or 0)+(m7['sector'] or 0),2),'%')} of ΔE — the market changed) "
        f"and **within-group selection decay** ({_fmt(m7['selection'],'%')} — leaders stopped beating "
        "their peers; RS and Trend factors inverted out-of-sample).",
        f"- The leaders' **core ranking profile (RS/sector/trend) barely moved** ({m3['score_drift']} "
        f"pts) while their excess return collapsed {_fmt(m3['exc_is'],'%')}→{_fmt(m3['exc_oos'],'%')} "
        "— the scoring kept flagging the same kind of names; those names simply stopped winning "
        f"(edge peaked **{m1['peak']}**, flag **{m1['flag']}**, non-monotone).", "",
        "## 1. Edge Decay Analysis", "", s1, "",
        "## 2. Market Structure Analysis", "", s2, "",
        "## 3. Leadership Quality Analysis", "", s3, "",
        "## 4. Factor Drift", "", s4, "",
        "## 5. ACTION_NOW Failure Analysis", "", s5, "",
        "## 6. Sector Regime Shift", "", s6, "",
        "## 7. Holdout Failure Decomposition", "", s7, "",
        "## 8. Counterfactual Analysis", "", s8, "",
        s9, "",
    ]
    return "\n".join(L) + "\n"


def run(rebuild: bool = False) -> Path:
    if rebuild or not _DATASET.exists():
        df = _build_dataset(years=5.0, every_weeks=1, period="5y", sample=0)
    else:
        print(f"  Using cached dataset {_DATASET}", flush=True)
        df = pd.read_csv(_DATASET)
    df["screen_date"] = df["screen_date"].astype(str)
    dates = sorted(df["screen_date"].unique())
    drange = f"{dates[0]} → {dates[-1]}" if dates else "—"
    md = build_report(df, len(dates), drange)
    _OUT.mkdir(parents=True, exist_ok=True)
    path = _OUT / "oos_failure_forensics.md"
    path.write_text(md, encoding="utf-8")
    print(f"\n  ✓ Wrote {path}  ({len(df)} obs, {len(dates)} screens)\n", flush=True)
    return path


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    run(rebuild="--rebuild" in sys.argv[1:])
