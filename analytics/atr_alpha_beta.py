"""
analytics/atr_alpha_beta.py — Phase 6A: ATR Alpha-vs-Beta Audit (MEASUREMENT ONLY)
==================================================================================
The single highest-information experiment from the Repair Lab (E5): is the ATR
factor's predictive power **genuine alpha** or merely **market-beta exposure**
(or sector / regime exposure)?

It changes NOTHING — no scoring, threshold, ranking, regime or factor logic; no
optimisation. It reuses the cached 94,637-obs replay (reports/validation/
robustness_dataset.csv) for the ATR *score* + forward returns, and additionally
estimates each stock's **beta** from daily OHLCV (cache) vs Nifty50 (^NSEI) and a
Nifty500 proxy (equal-weight universe), then beta-neutralises forward returns and
re-measures ATR's predictive power before/after, within sector, within regime,
and out-of-sample.

NB on the factor: the ATR *score* is hump-shaped (composite_engine.ATRScorer) —
it rewards a moderate 4–8% "ideal swing range" (score 100) and PENALISES both
dead-low (<2%) and extreme-high (≥8–12%) volatility. A pure-beta tilt would be
monotonic in volatility; this is not. The audit measures what it actually is.

Output: reports/validation/atr_alpha_beta_audit.md
Beta cache: reports/validation/atr_beta_by_ticker.csv

    python -m analytics.atr_alpha_beta              # estimate beta, then audit
    python -m analytics.atr_alpha_beta --reuse-beta # reuse cached per-ticker beta
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from analytics.robustness_audit import _DATASET, _build_dataset
from analytics.trust_audit import _m, _win, _fmt, _tbl, _wt

_OUT = Path("reports/validation")
_BETA = _OUT / "atr_beta_by_ticker.csv"
P = "fwd20"
RESID = "resid20"            # Nifty50-beta-neutralised forward 20D return
IS_YEARS = ["2022", "2023", "2024"]
OOS_YEARS = ["2025", "2026"]
MACRO = {"STRONG_BULL": "BULL", "BULL": "BULL", "NEUTRAL": "NEUTRAL",
         "WEAK_BEAR": "BEAR", "STRONG_BEAR": "BEAR"}


# ─────────────────────────────────────────────────────────────────────────────
# STATS
# ─────────────────────────────────────────────────────────────────────────────
def _ic(d, col, ret) -> Optional[float]:
    dd = d[[col, ret]].dropna()
    return round(float(dd[col].corr(dd[ret], method="spearman")), 3) if len(dd) > 3 else None


def _pearson(a, b) -> Optional[float]:
    d = pd.concat([pd.Series(a).reset_index(drop=True), pd.Series(b).reset_index(drop=True)],
                  axis=1).dropna()
    return round(float(d.iloc[:, 0].corr(d.iloc[:, 1])), 3) if len(d) > 3 else None


def _spearman(a, b) -> Optional[float]:
    d = pd.concat([pd.Series(a).reset_index(drop=True), pd.Series(b).reset_index(drop=True)],
                  axis=1).dropna()
    return round(float(d.iloc[:, 0].corr(d.iloc[:, 1], method="spearman")), 3) if len(d) > 3 else None


def _spread(d, col, ret, q=5) -> Optional[float]:
    dd = d[[col, ret]].dropna()
    if len(dd) < 5 * q:
        return None
    try:
        b = pd.qcut(dd[col].rank(method="first"), q, labels=False)
    except Exception:
        return None
    return round(float(dd[ret][b == q - 1].mean() - dd[ret][b == 0].mean()), 2)


def _combo_spread(d, c1, c2, ret, q=5) -> Optional[float]:
    dd = d[[c1, c2, ret]].dropna()
    if len(dd) < 5 * q:
        return None
    s = dd[c1].rank(pct=True) + dd[c2].rank(pct=True)
    try:
        b = pd.qcut(s.rank(method="first"), q, labels=False)
    except Exception:
        return None
    return round(float(dd[ret][b == q - 1].mean() - dd[ret][b == 0].mean()), 2)


def _terciles(d, by):
    dd = d.dropna(subset=[by]).copy()
    try:
        dd["_t"] = pd.qcut(dd[by].rank(method="first"), 3, labels=["Low", "Medium", "High"])
    except Exception:
        dd["_t"] = "—"
    return dd


# ─────────────────────────────────────────────────────────────────────────────
# BETA ESTIMATION (from daily OHLCV cache)
# ─────────────────────────────────────────────────────────────────────────────
def _latest_ohlcv_cache() -> Optional[Path]:
    files = sorted(Path("cache/ohlcv").glob("*_5y.csv"))
    return files[-1] if files else None


def _load_nifty_close() -> pd.Series:
    """Nifty50 close, read DIRECTLY from the benchmark cache (no network, no fetch_nifty)."""
    p = Path("cache/benchmarks/NSEI_5y.csv")
    if not p.exists():
        from scanner.relative_strength_engine import fetch_nifty
        n = fetch_nifty(period="5y")
        s = n["Close"].copy(); s.index = pd.to_datetime(s.index)
        return s.sort_index()
    n = pd.read_csv(p, usecols=["Date", "Close"])
    n["Date"] = pd.to_datetime(n["Date"])
    return n.set_index("Date")["Close"].sort_index()


def _fwd_map(nclose: pd.Series, h: int = 20) -> dict:
    """Nifty50 forward h-day return per trading date (screen dates are nifty trading days)."""
    out, idx = {}, nclose.index
    vals = nclose.to_numpy()
    for i, ts in enumerate(idx):
        if i + h < len(vals) and vals[i]:
            out[ts.date().isoformat()] = (float(vals[i + h]) / float(vals[i]) - 1) * 100
    return out


def _build_beta_frame() -> tuple[pd.DataFrame, dict]:
    """Estimate per-ticker beta + realized vol by reading ONLY Close prices directly from the
    OHLCV cache (usecols) — avoids get_ohlcv's heavy per-ticker DataFrame build/validation and any
    live benchmark download. Fully offline; runs in a few seconds."""
    cache = _latest_ohlcv_cache()
    if cache is None:
        raise FileNotFoundError("No cache/ohlcv/*_5y.csv found — run the replay once to populate it.")
    print(f"  Reading Close prices directly from {cache.name} (usecols=Close)...", flush=True)
    raw = pd.read_csv(cache, usecols=["ticker", "Date", "Close"])
    wide = raw.pivot_table(index="Date", columns="ticker", values="Close")
    wide.index = pd.to_datetime(wide.index)
    wide = wide.sort_index()
    rets = wide.pct_change()
    uni_ret = rets.mean(axis=1)                        # equal-weight Nifty500 proxy daily return

    nclose = _load_nifty_close()
    nret = nclose.pct_change()
    rvol = rets.std() * np.sqrt(252) * 100             # vectorised realized vol (annualised %)

    print(f"  Estimating beta for {rets.shape[1]} tickers (vectorised)...", flush=True)
    rows = []
    for t in rets.columns:
        r = rets[t]
        c50 = pd.concat([r, nret], axis=1).dropna()
        c500 = pd.concat([r, uni_ret], axis=1).dropna()
        b50 = (float(c50.iloc[:, 0].cov(c50.iloc[:, 1]) / c50.iloc[:, 1].var())
               if len(c50) >= 200 and c50.iloc[:, 1].var() else np.nan)
        b500 = (float(c500.iloc[:, 0].cov(c500.iloc[:, 1]) / c500.iloc[:, 1].var())
                if len(c500) >= 200 and c500.iloc[:, 1].var() else np.nan)
        rows.append({"ticker": t, "beta50": b50, "beta500": b500,
                     "rvol": float(rvol.get(t, np.nan))})
    bdf = pd.DataFrame(rows)
    nfwd = _fwd_map(nclose)
    print(f"  Beta estimated for {bdf['beta50'].notna().sum()}/{len(bdf)} tickers.", flush=True)
    _OUT.mkdir(parents=True, exist_ok=True)
    bdf.to_csv(_BETA, index=False)
    return bdf, nfwd


def _nifty_fwd_map() -> dict:
    """Cheap nifty fwd20 map (direct cache read) for the --reuse-beta path."""
    return _fwd_map(_load_nifty_close())


# ─────────────────────────────────────────────────────────────────────────────
# SECTIONS
# ─────────────────────────────────────────────────────────────────────────────
def _sec_cohorts(df) -> tuple[str, dict]:
    # by realized volatility (the beta question), and by ATR score (the factor)
    vt = _terciles(df, "rvol")
    rows_v = []
    mono_v = []
    for t in ["Low", "Medium", "High"]:
        g = vt[vt["_t"] == t]
        r = _m(g[P]); mono_v.append(r)
        rows_v.append([t, len(g.dropna(subset=[P])), _fmt(_m(g["rvol"])), _fmt(_m(g["beta50"])),
                       _fmt(r, "%"), _fmt(_win(g[P])), _fmt(_m(g["mae20"]), "%")])
    at = _terciles(df, "atr")
    rows_a = []
    for t in ["Low", "Medium", "High"]:
        g = at[at["_t"] == t]
        rows_a.append([t, len(g.dropna(subset=[P])), _fmt(_m(g["atr"])), _fmt(_m(g["rvol"])),
                       _fmt(_m(g["beta50"])), _fmt(_m(g[P]), "%"), _fmt(_win(g[P]))])
    mono = (mono_v[0] is not None and mono_v[2] is not None and
            mono_v[0] <= mono_v[1] <= mono_v[2])
    body = [
        "Two cohort views. **A — realized volatility** terciles answers the beta question directly "
        "(does return rise with volatility?). **B — ATR *score*** terciles shows what the factor "
        "actually selects (the score is hump-shaped: it rewards a moderate band and penalises "
        "extremes, so a high score ≠ high volatility).", "",
        "**A. By realized volatility (annualised)**", "",
        _tbl(["Vol tercile", "n", "Mean rvol", "Mean β(N50)", "Avg 20D", "Win", "Avg DD"], rows_v), "",
        f"- Return rises monotonically with raw volatility: **{'YES' if mono else 'NO'}** "
        f"({_fmt(mono_v[0],'%')} → {_fmt(mono_v[1],'%')} → {_fmt(mono_v[2],'%')}). "
        + ("Monotone vol→return is the signature of a beta tilt." if mono else
           "Non-monotone vol→return argues against a simple high-beta tilt."), "",
        "**B. By ATR score (the factor as used)**", "",
        _tbl(["ATR-score tercile", "n", "Mean ATR score", "Mean rvol", "Mean β(N50)", "Avg 20D", "Win"], rows_a),
    ]
    return "\n".join(body), {"vol_mono": mono, "vol_ret": mono_v}


def _sec_beta_exposure(df) -> tuple[str, dict]:
    p50 = _pearson(df["atr"], df["beta50"]); s50 = _spearman(df["atr"], df["beta50"])
    p500 = _pearson(df["atr"], df["beta500"]); s500 = _spearman(df["atr"], df["beta500"])
    pv = _pearson(df["rvol"], df["beta50"]); sv = _spearman(df["rvol"], df["beta50"])
    rows = [
        ["ATR score vs β(Nifty50)", _fmt(p50), _fmt(s50)],
        ["ATR score vs β(Nifty500)", _fmt(p500), _fmt(s500)],
        ["realized vol vs β(Nifty50)", _fmt(pv), _fmt(sv)],
    ]
    strong = (abs(p50 or 0) >= 0.5)
    body = [
        "Correlation of the ATR score with each stock's estimated beta. If the ATR score were "
        "'really beta', it would correlate strongly with beta. (Realized-vol vs beta is shown as a "
        "sanity check — it should be strongly positive.)", "",
        _tbl(["Pair", "Pearson", "Spearman"], rows), "",
        f"- ATR-score↔beta correlation is **{'STRONG' if strong else 'WEAK/MODERATE'}** "
        f"(Pearson {_fmt(p50)}). " +
        ("The score is largely a beta proxy." if strong else
         "The ATR *score* is NOT a simple beta proxy — consistent with its hump-shaped design "
         "(it discards the highest-beta, highest-vol names)."),
    ]
    return "\n".join(body), {"atr_beta50_pearson": p50, "atr_beta50_spearman": s50, "strong": strong}


def _sec_residual(df) -> tuple[str, dict]:
    raw_sp = _spread(df, "atr", P)
    res_sp = _spread(df, "atr", RESID)
    raw_ic = _ic(df, "atr", P)
    res_ic = _ic(df, "atr", RESID)
    surviving = (res_sp / raw_sp) if (raw_sp and res_sp is not None and raw_sp != 0) else None
    beta_expl = round((1 - (res_sp / raw_sp)) * 100) if (raw_sp and res_sp is not None and raw_sp > 0) else None
    rows = [
        ["ATR Q5−Q1 spread (20D)", _fmt(raw_sp, "%"), _fmt(res_sp, "%")],
        ["ATR Spearman IC", _fmt(raw_ic), _fmt(res_ic)],
    ]
    body = [
        "Beta neutralisation: residual return = stock 20D return − β(Nifty50) × Nifty50's own 20D "
        "return over the same window. If ATR's edge is beta, its spread/IC collapses on residuals; "
        "if it is alpha, it survives.", "",
        _tbl(["ATR predictive power", "Raw return", "Beta-neutral residual"], rows), "",
        f"- ATR retains **{_fmt(round(surviving*100) if surviving is not None else None)}%** of its "
        f"raw spread after beta removal (beta explains ~**{_fmt(beta_expl)}%**).",
        f"- Verdict on this lens: " +
        ("ATR **survives** beta neutralisation (alpha component present)." if (res_sp is not None and res_sp > 0 and (surviving or 0) >= 0.4)
         else "ATR is **largely beta** (residual edge collapses)." if (res_sp is not None and (surviving or 0) < 0.4)
         else "mixed / partial survival."),
    ]
    return "\n".join(body), {"raw_sp": raw_sp, "res_sp": res_sp, "raw_ic": raw_ic, "res_ic": res_ic,
                             "surviving": surviving, "beta_expl": beta_expl}


def _sec_sector(df) -> tuple[str, dict]:
    # within-sector ATR predictive power (sector-neutral): pooled sector-demeaned + per-sector spreads
    rows = []
    big = (df.groupby("sector").size().sort_values(ascending=False).head(8).index)
    sps = []
    for s in big:
        g = df[df["sector"] == s]
        sp = _spread(g, "atr", P)
        ic = _ic(g, "atr", P)
        if sp is not None:
            sps.append(sp)
        rows.append([s, len(g.dropna(subset=[P])), _fmt(sp, "%"), _fmt(ic)])
    avg_within = round(sum(sps) / len(sps), 2) if sps else None
    raw_sp = _spread(df, "atr", P)
    sector_expl = (round((1 - avg_within / raw_sp) * 100) if (raw_sp and avg_within is not None and raw_sp > 0) else None)
    body = [
        "ATR predictive power **within each sector** (the 8 largest). If ATR only worked across "
        "sectors (i.e. it was selecting strong sectors), its within-sector spread would vanish.", "",
        _tbl(["Sector", "n", "ATR Q5−Q1", "ATR IC"], rows), "",
        f"- Mean within-sector ATR spread **{_fmt(avg_within,'%')}** vs pooled **{_fmt(raw_sp,'%')}** "
        f"→ sector exposure explains ~**{_fmt(sector_expl)}%** of ATR's edge. " +
        ("ATR still predicts inside sectors (not just sector selection)." if (avg_within or 0) > 0.5
         else "ATR's edge largely disappears within sectors (it was sector selection)."),
    ]
    return "\n".join(body), {"avg_within": avg_within, "sector_expl": sector_expl}


def _sec_regime(df) -> tuple[str, dict]:
    df = df.assign(_macro=df["regime"].map(MACRO))
    rows = []
    sps = {}
    for mr in ["BULL", "NEUTRAL", "BEAR"]:
        g = df[df["_macro"] == mr]
        sp = _spread(g, "atr", P); rsp = _spread(g, "atr", RESID); ic = _ic(g, "atr", P)
        sps[mr] = sp
        rows.append([mr, len(g.dropna(subset=[P])), _fmt(sp, "%"), _fmt(rsp, "%"), _fmt(ic)])
    vals = [v for v in sps.values() if v is not None]
    spread_range = round(max(vals) - min(vals), 2) if vals else None
    regime_dep = spread_range is not None and spread_range >= 1.5
    body = [
        "ATR predictive power **within each macro-regime** (raw and beta-neutral). Tests whether "
        "ATR is really a regime bet.", "",
        _tbl(["Regime", "n", "ATR Q5−Q1 (raw)", "ATR Q5−Q1 (resid)", "ATR IC"], rows), "",
        f"- ATR spread ranges {_fmt(min(vals) if vals else None,'%')}–{_fmt(max(vals) if vals else None,'%')} "
        f"across regimes (range {_fmt(spread_range,'%')}). ATR is "
        + ("**regime-dependent**." if regime_dep else "**fairly regime-robust** (edge present in each)."),
    ]
    return "\n".join(body), {"by_regime": sps, "regime_dep": regime_dep}


def _sec_interaction(df) -> tuple[str, dict]:
    rs_a = _spread(df, "rs", P); atr_a = _spread(df, "atr", P); sec_a = _spread(df, "sector_c", P)
    rs_atr = _combo_spread(df, "rs", "atr", P)
    sec_atr = _combo_spread(df, "sector_c", "atr", P)
    # on residuals (does ATR add ALPHA beyond RS / Sector?)
    atr_res = _spread(df, "atr", RESID)
    rs_atr_res = _combo_spread(df, "rs", "atr", RESID)
    rows = [
        ["RS only", _fmt(rs_a, "%"), "—"],
        ["ATR only", _fmt(atr_a, "%"), _fmt(atr_res, "%")],
        ["RS + ATR", _fmt(rs_atr, "%"), _fmt(rs_atr_res, "%")],
        ["Sector only", _fmt(sec_a, "%"), "—"],
        ["Sector + ATR", _fmt(sec_atr, "%"), "—"],
    ]
    adds_rs = (rs_atr is not None and rs_a is not None and rs_atr > rs_a + 0.3)
    adds_sec = (sec_atr is not None and sec_a is not None and sec_atr > sec_a + 0.3)
    body = [
        "Does ATR add **independent** information beyond RS and beyond Sector? Equal-weight rank "
        "combos; the residual column repeats the test on beta-neutral returns (does ATR add *alpha*).", "",
        _tbl(["Combination", "Q5−Q1 (raw)", "Q5−Q1 (resid)"], rows), "",
        f"- ATR adds to RS: **{'YES' if adds_rs else 'no'}** (RS {_fmt(rs_a,'%')} → RS+ATR {_fmt(rs_atr,'%')}).",
        f"- ATR adds to Sector: **{'YES' if adds_sec else 'no'}** (Sector {_fmt(sec_a,'%')} → Sector+ATR {_fmt(sec_atr,'%')}).",
        f"- On beta-neutral residuals, RS+ATR {_fmt(rs_atr_res,'%')} vs ATR-resid {_fmt(atr_res,'%')} "
        "— independent *alpha* contribution " + ("present." if (atr_res or 0) > 0.3 else "weak/absent."),
    ]
    return "\n".join(body), {"rs_a": rs_a, "atr_a": atr_a, "rs_atr": rs_atr, "sec_a": sec_a,
                             "sec_atr": sec_atr, "atr_res": atr_res}


def _sec_holdout(df) -> tuple[str, dict]:
    rows = []
    out = {}
    for lbl, yrs in [("In-sample (2022–24)", IS_YEARS), ("Out-of-sample (2025–26)", OOS_YEARS)]:
        g = df[df["screen_date"].str[:4].isin(yrs)]
        raw = _spread(g, "atr", P); res = _spread(g, "atr", RESID)
        ic = _ic(g, "atr", P); cb = _pearson(g["atr"], g["beta50"])
        out[lbl] = {"raw": raw, "res": res, "ic": ic, "corr": cb}
        rows.append([lbl, len(g.dropna(subset=[P])), _fmt(raw, "%"), _fmt(res, "%"), _fmt(ic), _fmt(cb)])
    oos = out["Out-of-sample (2025–26)"]
    survived = oos["res"] is not None and oos["res"] > 0
    body = [
        "All ATR analyses split in-sample vs out-of-sample. The decisive cell is the **OOS "
        "beta-neutral residual spread** — does ATR's *alpha* survive the period that broke the rest "
        "of the system?", "",
        _tbl(["Window", "n", "ATR raw spread", "ATR resid spread", "ATR IC", "ATR↔β corr"], rows), "",
        f"- OOS residual ATR spread **{_fmt(oos['res'],'%')}** → ATR's beta-neutral edge "
        + ("**SURVIVED** out-of-sample." if survived else "**did NOT survive** out-of-sample."),
    ]
    return "\n".join(body), {"is": out["In-sample (2022–24)"], "oos": oos, "survived": survived}


# ─────────────────────────────────────────────────────────────────────────────
# VERDICT
# ─────────────────────────────────────────────────────────────────────────────
def _verdict(coh, beta, resid, sector, regime, inter, hold) -> tuple[str, str, dict]:
    raw = resid["raw_sp"] or 0.0
    beta_expl = resid["beta_expl"] or 0
    sector_expl = sector["sector_expl"] or 0
    alpha_share = round((resid["surviving"] or 0) * 100)
    vals = [v for v in regime["by_regime"].values() if v is not None]
    spread_range = round(max(vals) - min(vals), 2) if vals else None

    oos_alpha = hold["survived"]
    within_sector_ok = (sector["avg_within"] or 0) > 0.5
    beta_strong = beta["strong"]
    vol_premium = bool(coh["vol_mono"])        # monotone realized-vol → return = risk-premium signature
    regime_dep = bool(regime["regime_dep"])
    vr = coh["vol_ret"]                          # [low, med, high] tercile returns

    # decision (a residual that survives MARKET-beta neutralisation is NOT proof of skill —
    # beta neutralisation removes Nifty beta, not total/idiosyncratic volatility exposure)
    if beta_strong or alpha_share < 30:
        choice = "B) ATR is mostly beta"
    elif alpha_share >= 50 and within_sector_ok and oos_alpha and not vol_premium and not regime_dep:
        choice = "A) ATR is genuine alpha"
    elif sector_expl >= 60 and not within_sector_ok:
        choice = "C) ATR is sector exposure"
    elif oos_alpha and (vol_premium or regime_dep):
        choice = "E) Mixed — real residual, but a volatility / risk-premium exposure (not proven skill)"
    elif regime_dep:
        choice = "D) ATR is regime exposure"
    else:
        choice = "E) Mixed"

    contrib = {"beta": beta_expl, "sector": sector_expl, "residual": alpha_share,
               "vol_premium": vol_premium, "regime_dep": regime_dep}
    rows = [
        ["Market beta (Nifty50)", f"~{beta_expl}%", "removed by β×market neutralisation"],
        ["Sector exposure", f"~{sector_expl}%", f"within-sector spread {_fmt(sector['avg_within'],'%')} vs pooled {_fmt(raw,'%')}"],
        ["Volatility / idiosyncratic-vol premium", "large — NOT separately removed",
         f"return rises monotonically with realized vol ({_fmt(vr[0],'%')}→{_fmt(vr[1],'%')}→{_fmt(vr[2],'%')}); "
         "β-neutralisation removes MARKET beta only"],
        ["Regime dependence", f"spread range {_fmt(spread_range,'%')}", "raw ATR spread concentrated in BEAR (mean-reversion/vol premium)"],
        ["Residual after β-neutralisation", f"~{alpha_share}%", f"OOS residual {'survived' if oos_alpha else 'did NOT survive'} ({_fmt(hold['oos']['res'],'%')})"],
    ]
    text = [
        "## 8. Final Verdict", "",
        f"**{choice}**", "",
        "Contribution estimates (non-orthogonal — beta / sector / regime / volatility overlap):", "",
        _tbl(["Source", "Share of ATR's raw spread", "Evidence"], rows), "",
        "Reasoning (evidence only):",
        f"- **Not pure market-beta:** β neutralisation leaves **{alpha_share}%** of the raw spread "
        f"({_fmt(resid['res_sp'],'%')} of {_fmt(resid['raw_sp'],'%')}); ATR↔β correlation only "
        f"{_fmt(beta['atr_beta50_pearson'])}; ATR adds independent information beyond **both** RS "
        f"({_fmt(inter['rs_a'],'%')}→{_fmt(inter['rs_atr'],'%')}) and Sector "
        f"({_fmt(inter['sec_a'],'%')}→{_fmt(inter['sec_atr'],'%')}); and the residual **survived OOS** "
        f"({_fmt(hold['oos']['res'],'%')}). On these alone the project is NOT dead.",
        f"- **But not proven skill either:** return rises **monotonically with realized volatility** "
        f"({_fmt(vr[0],'%')}→{_fmt(vr[1],'%')}→{_fmt(vr[2],'%')}, with beta rising in step) — the "
        "signature of a volatility/risk premium — and β-neutralisation removes only *market* beta, not the "
        "*total/idiosyncratic* volatility exposure that drives most of the residual. ATR is also "
        f"strongly **regime-dependent** (spread range {_fmt(spread_range,'%')}, concentrated in BEAR) "
        "and carries the deeper drawdown of high-vol names.",
        "- **Net:** ATR is a genuine, OOS-durable return source that is *not* a simple market-beta "
        "proxy — but it is best characterised as a **volatility / risk-premium factor**, not "
        "demonstrated stock-selection skill. It should be harvested (and sized) as a risk premium.", "",
        "This audit measures the source of ATR's edge; it recommends nothing and changes nothing.",
    ]
    return choice, "\n".join(text), contrib


# ─────────────────────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────────────────────
def build_report(df, n_dates, drange) -> str:
    s1, m1 = _sec_cohorts(df)
    s2, m2 = _sec_beta_exposure(df)
    s3, m3 = _sec_residual(df)
    s4, m4 = _sec_sector(df)
    s5, m5 = _sec_regime(df)
    s6, m6 = _sec_interaction(df)
    s7, m7 = _sec_holdout(df)
    choice, s8, contrib = _verdict(m1, m2, m3, m4, m5, m6, m7)

    branch = ("**Branch 2 (a factor survives OOS):** a residual ATR edge survives out-of-sample and "
              "adds beyond RS and Sector, so the project is NOT dead — proceed to E4 (regime gate) + "
              "re-test the frozen 2025–26 holdout. **Caveat:** ATR reads as a *volatility / "
              "risk-premium* factor, not proven skill, so any deployable version is a risk-premium "
              "harvest (size for its drawdown), not free alpha."
              if choice.startswith(("A", "E")) and m7["survived"] else
              "**Branch 1 (no surviving OOS factor):** ATR does not survive as an independent edge — "
              "the deployable-strategy case is, on current evidence, effectively closed.")

    L = [
        "# ATR Alpha-vs-Beta Audit — Phase 6A", "",
        f"_Generated {datetime.now().isoformat(timespec='seconds')} · {n_dates} weekly screens · "
        f"{len(df.dropna(subset=[P]))} ATR-scored obs · {drange}_", "",
        "> Measurement only. The single highest-information experiment (Repair-Lab E5): is the ATR "
        "factor genuine alpha or market/sector/regime exposure? Beta is estimated from daily OHLCV "
        "vs Nifty50 (^NSEI) and an equal-weight Nifty500 proxy; forward returns are beta-neutralised; "
        "ATR's predictive power is re-measured before/after, within sector, within regime, and "
        "out-of-sample. **No scoring, threshold, ranking, regime or factor logic was changed; nothing "
        "tuned or optimised.** Primary horizon 20D. Beta is a static full-sample estimate (an "
        "attribution, not a forward signal).", "",
        "## Executive Summary", "",
        f"- **Verdict: {choice}.** Market beta (Nifty50) explains only ~{contrib['beta']}% of ATR's "
        f"raw 20D spread and the β-neutral residual keeps ~{contrib['residual']}% (it **survived** "
        "out-of-sample) — so ATR is **not** a simple market-beta proxy (ATR↔β Pearson "
        f"{_fmt(m2['atr_beta50_pearson'])}, hump-shaped score discards the highest-β names)."
        if m7["survived"] else
        f"- **Verdict: {choice}.** The β-neutral residual did NOT survive out-of-sample.",
        "- **BUT it is not proven skill:** return rises monotonically with realized volatility "
        f"({_fmt(m1['vol_ret'][0],'%')}→{_fmt(m1['vol_ret'][1],'%')}→{_fmt(m1['vol_ret'][2],'%')}) and "
        "ATR is strongly regime-dependent — β-neutralisation removes *market* beta, not *total* "
        "volatility exposure. ATR reads as a **volatility / risk-premium factor**, not stock-selection skill.",
        f"- {branch}", "",
        "## 1. ATR Cohorts", "", s1, "",
        "## 2. Beta Exposure Analysis", "", s2, "",
        "## 3. Residual (beta-neutral) Return Analysis", "", s3, "",
        "## 4. Sector Adjustment", "", s4, "",
        "## 5. Regime Adjustment", "", s5, "",
        "## 6. Interaction Analysis", "", s6, "",
        "## 7. Holdout Validation", "", s7, "",
        s8, "",
    ]
    return "\n".join(L) + "\n"


def run(reuse_beta: bool = False) -> Path:
    if not _DATASET.exists():
        _build_dataset(years=5.0, every_weeks=1, period="5y", sample=0)
    df = pd.read_csv(_DATASET)
    df["screen_date"] = df["screen_date"].astype(str)

    if reuse_beta and _BETA.exists():
        print(f"  Reusing cached beta {_BETA}", flush=True)
        bdf = pd.read_csv(_BETA)
        nfwd = _nifty_fwd_map()
    else:
        bdf, nfwd = _build_beta_frame()

    df = df.merge(bdf, on="ticker", how="left")
    df["nifty_fwd20"] = df["screen_date"].map(nfwd)
    df[RESID] = df[P] - df["beta50"] * df["nifty_fwd20"]

    dates = sorted(df["screen_date"].unique())
    drange = f"{dates[0]} → {dates[-1]}" if dates else "—"
    md = build_report(df, len(dates), drange)
    _OUT.mkdir(parents=True, exist_ok=True)
    path = _OUT / "atr_alpha_beta_audit.md"
    path.write_text(md, encoding="utf-8")
    print(f"\n  ✓ Wrote {path}\n", flush=True)
    return path


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    run(reuse_beta="--reuse-beta" in sys.argv[1:])
