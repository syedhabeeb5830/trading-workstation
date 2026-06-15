"""
analytics/edge_attribution.py — Edge Attribution Forensic Audit
===============================================================
EXPLANATORY / MEASUREMENT ONLY. Replays the screener exactly as it exists today,
capturing the per-stock COMPONENT scores (rs/sector/breakout/trend/liquidity/
freshness/atr) plus composite + actionability, and measures each factor's
independent and marginal predictive value against forward returns.

It changes NO logic, tunes NO parameters, recommends NO changes. It only answers:
"which components actually create predictive power, and which are noise?"

Output: reports/validation/edge_attribution_audit.md

    python -m analytics.edge_attribution [years] [every_weeks] [sample]
"""
from __future__ import annotations

import statistics as st
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from scanner.sector_engine import SectorRanker
from scanner.relative_strength_engine import RelativeStrengthRanker, fetch_nifty
from scanner.composite_engine import CompositeRanker
from scanner.actionability_engine import ActionabilityRanker
from scanner.regime_engine import RegimeClassifier
from analytics.rolling_validation import _weekly_asofs, _welch_t

_OUT = Path("reports/validation")
HORIZONS = [5, 10, 20, 60, 120]
PRIMARY = 20

# factor display-name → dataframe column
FACTORS = {
    "Relative Strength": "rs",
    "Sector": "sector_c",
    "Breakout Quality": "breakout",
    "Trend Score": "trend",
    "Liquidity Score": "liquidity",
    "Freshness Score": "freshness",
    "ATR Score": "atr",
    "Composite Score": "composite",
    "Actionability Score": "actionability",
}
COMPONENTS = ["rs", "sector_c", "breakout", "trend", "liquidity", "atr", "freshness"]
STRONG_REGIMES = {"STRONG_BULL", "BULL"}
WEAK_REGIMES = {"WEAK_BEAR", "STRONG_BEAR"}


# ─────────────────────────────────────────────────────────────────────────────
# REPLAY CAPTURING COMPONENTS
# ─────────────────────────────────────────────────────────────────────────────
def _replay_components(ohlcv_full, sector_map, nifty_full, as_of, min_bars=60) -> list[dict]:
    sliced = {t: df[df.index <= as_of] for t, df in ohlcv_full.items()}
    sliced = {t: d for t, d in sliced.items() if len(d) >= min_bars}
    if not sliced:
        return []
    nifty = nifty_full[nifty_full.index <= as_of] if nifty_full is not None else None

    sector_snap = SectorRanker(sector_map, sliced).rank()
    rs_snap = RelativeStrengthRanker(sliced, sector_map, nifty, sector_snap).rank()
    comp_snap = CompositeRanker(sliced, sector_map, sector_snap, rs_snap).rank()
    act_snap = ActionabilityRanker(comp_snap, sliced).rank()
    regime = RegimeClassifier().analyze(
        ohlcv=sliced, index_dfs={"Nifty50": nifty}, sector_snapshot=sector_snap,
        rs_snapshot=rs_snap, composite_snapshot=comp_snap, actionability_snapshot=act_snap)

    sd = as_of.date().isoformat()
    rows = []
    for cr in comp_snap.rows:
        a = act_snap.get(cr.ticker)
        comp = cr.components
        rows.append({
            "screen_date": sd, "ticker": cr.ticker, "sector": cr.sector,
            "regime": regime.regime, "grade": cr.grade, "rs_status": cr.rs_status,
            "classification": a.classification if a else "AVOID",
            "composite": cr.score,
            "actionability": a.actionability_score if a else 0.0,
            "rs": comp.get("rs"), "sector_c": comp.get("sector"),
            "breakout": comp.get("breakout"), "trend": comp.get("trend"),
            "liquidity": comp.get("liquidity"), "atr": comp.get("atr"),
            "freshness": comp.get("freshness"),
        })
    return rows


def _attach_forward(rows, ohlcv) -> None:
    by_ticker: dict[str, list[dict]] = {}
    for r in rows:
        by_ticker.setdefault(r["ticker"], []).append(r)
    for t, rs in by_ticker.items():
        df = ohlcv.get(t)
        if df is None or df.empty:
            for r in rs:
                for h in HORIZONS:
                    r[f"fwd{h}"] = None
                r["mae20"] = None
            continue
        idx = df.index
        for r in rs:
            pos = int(idx.searchsorted(pd.Timestamp(r["screen_date"]), side="right")) - 1
            base = float(df["Close"].iloc[pos]) if 0 <= pos < len(df) else 0.0
            for h in HORIZONS:
                j = pos + h
                r[f"fwd{h}"] = (round((float(df["Close"].iloc[j]) / base - 1) * 100, 2)
                               if base > 0 and 0 <= pos and j < len(df) else None)
            # 20D max adverse excursion
            if base > 0 and 0 <= pos:
                seg = df.iloc[pos + 1: min(pos + PRIMARY, len(df) - 1) + 1]
                r["mae20"] = round((float(seg["Low"].min()) / base - 1) * 100, 2) if not seg.empty else None
            else:
                r["mae20"] = None


# ─────────────────────────────────────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────────────────────────────────────
def _spread(df, col, ret="fwd20", q=5) -> Optional[float]:
    """Top-quintile minus bottom-quintile mean forward return."""
    d = df[[col, ret]].dropna()
    if len(d) < 5 * q:
        return None
    try:
        d = d.assign(_b=pd.qcut(d[col].rank(method="first"), q, labels=False))
    except Exception:
        return None
    top = d[d["_b"] == q - 1][ret].mean()
    bot = d[d["_b"] == 0][ret].mean()
    return round(top - bot, 2)


def _combo_spread(df, c1, c2, ret="fwd20", q=5) -> Optional[float]:
    d = df[[c1, c2, ret]].dropna()
    if len(d) < 5 * q:
        return None
    score = d[c1].rank(pct=True) + d[c2].rank(pct=True)
    try:
        b = pd.qcut(score.rank(method="first"), q, labels=False)
    except Exception:
        return None
    top = d[b == q - 1][ret].mean()
    bot = d[b == 0][ret].mean()
    return round(top - bot, 2)


def _monotonic_quintiles(df, col, ret="fwd20", q=5):
    d = df[[col, ret]].dropna()
    if len(d) < 5 * q:
        return None, None
    d = d.assign(_b=pd.qcut(d[col].rank(method="first"), q, labels=False))
    means = [round(float(d[d["_b"] == i][ret].mean()), 2) for i in range(q)]
    mono = all(means[i] <= means[i + 1] for i in range(q - 1))
    return means, mono


def _pearson(df, col, ret="fwd20") -> Optional[float]:
    d = df[[col, ret]].dropna()
    return round(d[col].corr(d[ret]), 3) if len(d) > 3 else None


def _spearman(df, col, ret="fwd20") -> Optional[float]:
    d = df[[col, ret]].dropna()
    return round(d[col].corr(d[ret], method="spearman"), 3) if len(d) > 3 else None


def _stability(df, col, ret="fwd20") -> tuple[Optional[float], Optional[float]]:
    """Per-year spearman: (mean corr, fraction of years positive)."""
    df = df.assign(_yr=df["screen_date"].str[:4])
    cs = []
    for _, g in df.groupby("_yr"):
        c = _spearman(g, col, ret)
        if c is not None:
            cs.append(c)
    if not cs:
        return None, None
    return round(sum(cs) / len(cs), 3), round(sum(1 for c in cs if c > 0) / len(cs), 2)


def _fmt(v, s=""):
    return "—" if v is None or (isinstance(v, float) and v != v) else f"{v}{s}"


def _tbl(header, rows):
    L = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        L.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# BUILD REPORT
# ─────────────────────────────────────────────────────────────────────────────
def build_report(df: pd.DataFrame, n_dates: int, drange: str) -> str:
    # NB: 'sector' (string name) and 'sector_c' (numeric component score) are distinct.
    n = len(df)
    uni20 = round(df["fwd20"].dropna().mean(), 2)

    # ── per-factor independent value ─────────────────────────────────────────
    fac_rows, stab_rows, reg_rows = [], [], []
    spreads = {}
    regimes = sorted(df["regime"].unique())
    for name, col in FACTORS.items():
        if col not in df:
            continue
        pear, spear = _pearson(df, col), _spearman(df, col)
        means, mono = _monotonic_quintiles(df, col)
        sp = _spread(df, col)
        spreads[col] = sp
        mc, fp = _stability(df, col)
        fac_rows.append([name, _fmt(pear), _fmt(spear),
                         "YES" if mono else "no" if mono is not None else "—",
                         _fmt(sp, "%"), _fmt(means)])
        stab_rows.append([name, _fmt(mc), _fmt(fp), _fmt(sp, "%")])
        rs_by_regime = []
        for rg in regimes:
            rs_by_regime.append(f"{rg[:4]} {_fmt(_spearman(df[df['regime']==rg], col))}")
        reg_rows.append([name] + [_fmt(_spearman(df[df["regime"] == rg], col)) for rg in regimes])

    # ── marginal contribution ────────────────────────────────────────────────
    rs_alone = spreads.get("rs")
    sec_alone = spreads.get("sector_c")
    tr_alone = spreads.get("trend")
    full = spreads.get("composite")
    marg_rows = []
    for name, col in FACTORS.items():
        if col in ("composite",):
            continue
        alone = spreads.get(col)
        p_rs = _combo_spread(df, col, "rs")
        p_sec = _combo_spread(df, col, "sector_c")
        p_tr = _combo_spread(df, col, "trend")
        # classify vs RS (the proven base factor)
        verdict = "—"
        if alone is not None and p_rs is not None and rs_alone is not None:
            if col == "rs":
                verdict = "base factor"
            elif p_rs > max(rs_alone, alone) + 0.3:
                verdict = "ADDITIVE"
            elif p_rs < rs_alone - 0.3:
                verdict = "DILUTES"
            elif abs(p_rs - rs_alone) <= 0.3:
                verdict = "redundant w/ RS"
            if alone is not None and alone < 0:
                verdict = "CONTRADICTORY" if verdict == "—" else verdict
        marg_rows.append([name, _fmt(alone, "%"), _fmt(p_rs, "%"), _fmt(p_sec, "%"),
                          _fmt(p_tr, "%"), _fmt(full, "%"), verdict])

    # ── classification profile ───────────────────────────────────────────────
    cls_rows = []
    for c in ["ACTION_NOW", "WATCHLIST", "EXTENDED", "AVOID"]:
        g = df[df["classification"] == c]
        if g.empty:
            continue
        cls_rows.append([c, len(g.dropna(subset=["fwd20"])),
                         _fmt(round(g["fwd20"].dropna().mean(), 2), "%"),
                         _fmt(round((g["fwd20"].dropna() > 0).mean(), 3)),
                         _fmt(round(g["mae20"].dropna().mean(), 2), "%"),
                         _fmt(round(g["rs"].mean(), 0)), _fmt(round(g["composite"].mean(), 0)),
                         _fmt(round(g["actionability"].mean(), 0)),
                         _fmt(round(g["trend"].mean(), 0)), _fmt(round(g["breakout"].mean(), 0))])

    # ── grade profile + inversion driver ─────────────────────────────────────
    grades = ["A+", "A", "B+", "B", "C", "D"]
    gr_rows = []
    gmeans = {}
    for gr in grades:
        g = df[df["grade"] == gr]
        if g.empty:
            continue
        gmeans[gr] = round(g["fwd20"].dropna().mean(), 2) if g["fwd20"].notna().any() else None
        gr_rows.append([gr, len(g.dropna(subset=["fwd20"])), _fmt(gmeans[gr], "%")]
                       + [_fmt(round(g[c].mean(), 0)) for c in COMPONENTS])
    # which component is most non-monotonic across grades (inversion driver)?
    present = [gr for gr in grades if gr in gmeans and gmeans[gr] is not None]
    inv_driver = "—"
    if len(present) >= 3:
        comp_mono = {}
        for c in COMPONENTS:
            seq = [df[df["grade"] == gr][c].mean() for gr in present]
            # A+ should have the highest component; count inversions vs grade order
            invs = sum(1 for i in range(len(seq) - 1) if seq[i] < seq[i + 1])
            comp_mono[c] = invs
        inv_driver = ", ".join(f"{c}({v})" for c, v in
                               sorted(comp_mono.items(), key=lambda kv: -kv[1])[:3])

    # ── regime value ─────────────────────────────────────────────────────────
    regv_rows = []
    for rg in sorted(df["regime"].unique(),
                     key=lambda k: -(df[df["regime"]==k]["fwd20"].dropna().mean() if df[df["regime"]==k]["fwd20"].notna().any() else 0)):
        g = df[df["regime"] == rg]
        an = g[g["classification"] == "ACTION_NOW"]
        regv_rows.append([rg, len(g.dropna(subset=["fwd20"])),
                          _fmt(round(g["fwd20"].dropna().mean(), 2), "%"),
                          _fmt(round((g["fwd20"].dropna() > 0).mean(), 3)),
                          _fmt(round(g["mae20"].dropna().mean(), 2), "%"),
                          _fmt(round(an["fwd20"].dropna().mean(), 2), "%") if an["fwd20"].notna().any() else "—",
                          len(an.dropna(subset=["fwd20"]))])
    strong = df[df["regime"].isin(STRONG_REGIMES)]["fwd20"].dropna()
    weak = df[df["regime"].isin(WEAK_REGIMES)]["fwd20"].dropna()
    strong_m = round(strong.mean(), 2) if len(strong) else None
    weak_m = round(weak.mean(), 2) if len(weak) else None
    strong_dd = round(df[df["regime"].isin(STRONG_REGIMES)]["mae20"].dropna().mean(), 2) if len(strong) else None
    weak_dd = round(df[df["regime"].isin(WEAK_REGIMES)]["mae20"].dropna().mean(), 2) if len(weak) else None

    # ── rank predictors by independent spread for the summary ────────────────
    ranked = sorted(((name, spreads.get(col)) for name, col in FACTORS.items()
                     if spreads.get(col) is not None), key=lambda x: -x[1])

    L = [f"# Edge Attribution — Forensic Audit", "",
         f"_Generated {datetime.now().isoformat(timespec='seconds')} · {n_dates} weekly "
         f"screens · {n} observations · {drange}_", "",
         "> Explanatory / measurement only. Production Phase 2–7 engines replayed point-in-time; "
         "no scoring, threshold, regime, ranking or factor changed. Primary horizon 20D. "
         "'Spread' = top-quintile − bottom-quintile mean 20D return.", "",
         "## Executive Summary", "",
         f"- Universe 20D mean return: **{uni20}%** (n={int(df['fwd20'].notna().sum())}).",
         "- Factors ranked by independent edge (top−bottom quintile 20D spread): "
         + ", ".join(f"**{nm}** {sp:+.1f}%" for nm, sp in ranked) + ".",
         f"- Composite score's edge ({_fmt(full,'%')}) vs its strongest single input "
         f"(RS {_fmt(rs_alone,'%')}) shows how much the blend adds beyond RS.",
         f"- Grade non-monotonicity driver(s) (components most inverted vs A+→D order): {inv_driver}.",
         f"- Strong regimes {(_fmt(strong_m,'%'))} vs weak {(_fmt(weak_m,'%'))} "
         f"(drawdown {_fmt(strong_dd,'%')} vs {_fmt(weak_dd,'%')}).", "",
         "## Strongest / Weakest Predictors (independent value)", "",
         _tbl(["Factor", "Pearson", "Spearman", "Monotonic", "Q5−Q1 spread", "Quintile means (20D)"], fac_rows),
         "", "### Predictive stability (per-year Spearman)", "",
         _tbl(["Factor", "Mean yearly corr", "Frac years +", "Spread"], stab_rows),
         "", "### Regime sensitivity (Spearman within each regime)", "",
         _tbl(["Factor"] + list(regimes), reg_rows),
         "", "## Marginal Contribution (Q5−Q1 spread of the combined rank)", "",
         _tbl(["Factor", "Alone", "+RS", "+Sector", "+Trend", "Full(composite)", "Verdict vs RS"], marg_rows),
         "", "_ADDITIVE = combo beats both alone · redundant = combo ≈ RS alone · "
         "DILUTES = combo < RS alone · CONTRADICTORY = negative standalone edge._", "",
         "## Classification Analysis (factor profile + outcome)", "",
         _tbl(["Class", "n", "Avg 20D", "Win", "Avg DD", "RS", "Comp", "Act", "Trend", "Breakout"], cls_rows),
         "", "## Grade Analysis (component means by grade; inversion driver)", "",
         _tbl(["Grade", "n", "Avg 20D"] + [c[:4] for c in COMPONENTS], gr_rows),
         f"\n_Components most inverted vs the A+→D ordering (inversion count): **{inv_driver}**. "
         "A non-monotonic grade ladder means these components do not decline with grade as composite implies._", "",
         "## Regime Analysis (does conditioning add value?)", "",
         _tbl(["Regime", "n", "Avg 20D", "Win", "Avg DD", "ACTION_NOW 20D", "AN n"], regv_rows),
         "", "## Trust Assessment", "",
         _trust(ranked, rs_alone, full, strong_m, weak_m, uni20), ""]
    return "\n".join(L) + "\n"


def _trust(ranked, rs_alone, full, strong_m, weak_m, uni20) -> str:
    out = []
    if ranked:
        top = ranked[0]
        out.append(f"- The dominant independent predictor is **{top[0]}** (spread {top[1]:+.1f}%). "
                   "Predictive power is concentrated, not broad-based.")
    if rs_alone is not None and full is not None:
        delta = round(full - rs_alone, 2)
        out.append(f"- Composite blend spread {full:+.1f}% vs RS-alone {rs_alone:+.1f}% "
                   f"(Δ {delta:+.1f}%): the multi-factor blend "
                   + ("adds measurable separation beyond RS." if delta > 0.5
                      else "adds little beyond RS — most factors are riding the RS signal."))
    if strong_m is not None and weak_m is not None:
        out.append(f"- Regime conditioning: strong {strong_m}% vs weak {weak_m}% — "
                   + ("regime ordering is informative." if strong_m > weak_m
                      else "regime ordering is NOT monotone in this window (does not cleanly separate good/bad markets)."))
    out.append("- This is an attribution of WHERE the measured edge comes from. It is not a "
               "recommendation to add, drop, or reweight any factor — no change is proposed.")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────────────────
def run(years: float = 5.0, every_weeks: int = 1, period: str = "5y", sample: int = 0) -> Path:
    from scanner.universe_builder import build_universe
    from scanner.data_feed import get_ohlcv

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
    print(f"  Data: {len(feed.data)} tickers. Replaying {len(asofs)} weekly screens "
          f"(capturing components)...", flush=True)

    rows: list[dict] = []
    for i, ts in enumerate(asofs, 1):
        rows += _replay_components(feed.data, sector_map, nifty, ts)
        if i % 10 == 0 or i == len(asofs):
            print(f"    …replayed {i}/{len(asofs)}", flush=True)

    _attach_forward(rows, feed.data)
    df = pd.DataFrame(rows)
    dates = sorted(df["screen_date"].unique())
    drange = f"{dates[0]} → {dates[-1]}" if len(dates) else "—"
    md = build_report(df, len(dates), drange)

    _OUT.mkdir(parents=True, exist_ok=True)
    path = _OUT / "edge_attribution_audit.md"
    path.write_text(md, encoding="utf-8")
    print(f"\n  ✓ Wrote {path}  ({len(df)} obs, {len(dates)} screens)\n", flush=True)
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
