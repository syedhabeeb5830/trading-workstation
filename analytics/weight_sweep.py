"""
analytics/weight_sweep.py — Walk-Forward OOS Weight Sweep + 2025 Diagnostic
===========================================================================
Diagnoses the 2025 out-of-sample alpha drop and sweeps the regime adaptive
weights (RS / Sector / Breakout / Trend) — but evaluates every profile
*strictly out-of-sample*, the way this project's own §5 demands:

    "No optimization before a robustness audit. Optimize from OUTCOMES, not
     opinions. Validation precedes modification."

Why NOT a naive 3-month in-sample sweep: Phase H proved the 2025 collapse was
period-specific overfitting; Phase E proved Breakout is net-negative every year.
An in-sample optimizer would re-inflate Breakout on noise and hand back a
confident, overfit weight set — the single most dangerous artifact you could
trust with capital. So this harness REFUSES to recommend any profile whose
walk-forward OOS edge does not beat the NEUTRAL baseline with a bootstrap CI
that excludes zero.

Method (honest walk-forward):
  • Substrate: the cached 94k-row replay dataset (per-stock component scores +
    forward returns) — `reports/validation/robustness_dataset.csv`. No re-replay.
  • For each test YEAR Y: the optimizer may use ONLY data before Y to PICK the
    best profile (in-sample selection), then that profile is scored on Y (unseen).
  • Pool the chosen-profile OOS results across years → mean edge vs baseline,
    win-rate, profit factor, and a date-clustered bootstrap 95% CI.
  • Verdict ADOPT ⇔ pooled OOS edge > 0 AND CI excludes 0. Else: keep NEUTRAL.

This module CHANGES NO PRODUCTION SCORING. It is measurement + a gated
recommendation. Adopting a profile is a separate, manual act (edit
config/regime_profiles.yaml) and should itself be re-Trust-audited first.

CLI:
    python -m analytics.weight_sweep              # full sweep + diagnostic
    python -m analytics.weight_sweep --quick      # coarse grid (fast)
    python -m analytics.weight_sweep --top-n 5    # selection basket size
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

_log = logging.getLogger(__name__)

_DATASET = Path("reports/validation/robustness_dataset.csv")
_OUT     = Path("reports/validation/weight_sweep.json")

# Components in the adaptive score (must match the dataset columns).
_COMPONENTS = ["rs", "sector_c", "breakout", "trend", "liquidity", "atr", "freshness"]

# NEUTRAL baseline (mirrors adaptive_scoring._DEFAULT_PROFILES["NEUTRAL"]).
_BASELINE = {"rs": 25, "sector_c": 20, "breakout": 20, "trend": 15,
             "liquidity": 10, "atr": 5, "freshness": 5}

# Held-constant (non-swept) factors — Phase E shows these are not where the
# leverage is; we sweep only the four the user named.
_FIXED = {"liquidity": 10, "atr": 5, "freshness": 5}

_HORIZON = "fwd20"   # primary forward horizon (20 trading days)


# ─────────────────────────────────────────────────────────────────────────────
# DATA TYPES
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ProfileResult:
    weights:        dict[str, float]
    oos_edge:       float            # mean per-date edge vs baseline, pooled OOS
    oos_winrate:    float            # selected-name win rate (fwd20 > 0), OOS
    oos_pf:         float            # profit factor of selected names, OOS
    oos_mean_ret:   float            # mean fwd20 of selected names, OOS
    n_dates:        int

    def label(self) -> str:
        return (f"RS{int(self.weights['rs'])} Sec{int(self.weights['sector_c'])} "
                f"Brk{int(self.weights['breakout'])} Trn{int(self.weights['trend'])}")


@dataclass
class SweepReport:
    verdict:          str            # ADOPT <profile> | NO DEPLOYABLE PROFILE
    recommended:      Optional[dict]
    walk_forward:     dict           # pooled OOS metrics for the WF-chosen profiles
    baseline_oos:     dict
    per_year:         list[dict]     # what the optimizer chose each year + its OOS result
    grid_top:         list[dict]     # descriptive: best profiles by recent-holdout edge
    diagnostic_2025:  dict
    n_profiles:       int
    generated_at:     str = ""
    notes:            list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# CORE
# ─────────────────────────────────────────────────────────────────────────────
def load_dataset(path: Path = _DATASET) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — build it first with: python -m analytics.robustness_audit")
    df = pd.read_csv(path)
    missing = [c for c in _COMPONENTS + ["screen_date", _HORIZON, "classification"]
               if c not in df.columns]
    if missing:
        raise ValueError(f"dataset missing required columns: {missing}")
    df = df.dropna(subset=[_HORIZON]).copy()
    df["year"] = df["screen_date"].astype(str).str[:4].astype(int)
    return df


def _make_grid(quick: bool) -> list[dict[str, float]]:
    """Coarse, interpretable grid over the four swept factors. Includes
    breakout=0 (the 'drop the proven-dilutive factor' hypothesis from Phase E)."""
    if quick:
        rs_g, sec_g, brk_g, trn_g = [25, 30], [20, 25], [0, 20], [15, 25]
    else:
        rs_g  = [20, 25, 30, 35]
        sec_g = [15, 20, 25]
        brk_g = [0, 10, 20]
        trn_g = [10, 15, 25]
    grid = []
    for rs in rs_g:
        for sec in sec_g:
            for brk in brk_g:
                for trn in trn_g:
                    w = {"rs": rs, "sector_c": sec, "breakout": brk, "trend": trn}
                    w.update(_FIXED)
                    grid.append(w)
    # guarantee the exact baseline is present and first
    if _BASELINE not in grid:
        grid.insert(0, dict(_BASELINE))
    return grid


def _selected(df: pd.DataFrame, weights: dict[str, float], n: int,
              exclude_avoid: bool) -> pd.DataFrame:
    """Return the Top-N-by-adaptive-score rows per screen_date (vectorised).

    adaptive_score = Σ comp[k]·w[k] / Σw   (the production formula, re-weighted)."""
    pool = df[df["classification"] != "AVOID"] if exclude_avoid else df
    wsum = sum(weights.values()) or 1.0
    score = sum(pool[k].astype(float) * weights.get(k, 0.0) for k in _COMPONENTS) / wsum
    tmp = pool[["screen_date", "year", _HORIZON]].copy()
    tmp["score"] = score.values
    tmp = tmp.sort_values(["screen_date", "score"], ascending=[True, False])
    tmp["rk"] = tmp.groupby("screen_date").cumcount()
    return tmp[tmp["rk"] < n]


def _perdate_mean(sel: pd.DataFrame) -> pd.Series:
    """Mean fwd20 of the selected basket, per screen_date."""
    return sel.groupby("screen_date")[_HORIZON].mean()


def _winrate(sel: pd.DataFrame) -> float:
    return round(float((sel[_HORIZON] > 0).mean()) * 100, 1) if len(sel) else 0.0


def _profit_factor(sel: pd.DataFrame) -> float:
    r = sel[_HORIZON]
    gains = r[r > 0].sum()
    losses = abs(r[r < 0].sum())
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return round(float(gains / losses), 2)


def _edge_series(profile_perdate: pd.Series, base_perdate: pd.Series) -> pd.Series:
    """Per-date edge (profile − baseline) on the dates both cover."""
    common = profile_perdate.index.intersection(base_perdate.index)
    return (profile_perdate.loc[common] - base_perdate.loc[common]).dropna()


def _bootstrap_ci(edge: pd.Series, b: int = 2000, seed: int = 7) -> tuple[float, float]:
    """Date-clustered bootstrap CI on the mean per-date edge."""
    if len(edge) < 5:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    vals = edge.values
    means = [rng.choice(vals, size=len(vals), replace=True).mean() for _ in range(b)]
    return (round(float(np.percentile(means, 2.5)), 3),
            round(float(np.percentile(means, 97.5)), 3))


# ─────────────────────────────────────────────────────────────────────────────
# 2025 DIAGNOSTIC  (reproduces Phase H lens 4: factor ICs invert OOS)
# ─────────────────────────────────────────────────────────────────────────────
def _rank_ic(df: pd.DataFrame, col: str) -> float:
    """Spearman rank IC of a component vs fwd20 (rank-Pearson, no scipy dep)."""
    sub = df[[col, _HORIZON]].dropna()
    if len(sub) < 50:
        return float("nan")
    return round(float(sub[col].rank().corr(sub[_HORIZON].rank())), 4)


def diagnose_oos(df: pd.DataFrame, split_year: int = 2025) -> dict:
    """Why did the edge drop? Compare each swept factor's IC in-sample (< split)
    vs out-of-sample (≥ split). A sign flip = the factor stopped predicting."""
    is_df = df[df["year"] < split_year]
    oos_df = df[df["year"] >= split_year]
    factors = ["rs", "sector_c", "breakout", "trend", "atr", "liquidity"]
    rows = []
    for f in factors:
        ic_is = _rank_ic(is_df, f)
        ic_oos = _rank_ic(oos_df, f)
        flipped = (not np.isnan(ic_is) and not np.isnan(ic_oos)
                   and np.sign(ic_is) != np.sign(ic_oos))
        rows.append({"factor": f, "ic_in_sample": ic_is, "ic_oos": ic_oos,
                     "delta": (round(ic_oos - ic_is, 4)
                               if not (np.isnan(ic_is) or np.isnan(ic_oos)) else None),
                     "sign_flip": bool(flipped)})
    # regime mix shift (corroborates "market changed")
    def _bull_share(d):
        if "regime" not in d or d.empty:
            return None
        return round(float(d["regime"].astype(str).str.contains("BULL").mean()), 3)
    return {
        "split_year": split_year,
        "n_in_sample": int(len(is_df)),
        "n_oos": int(len(oos_df)),
        "factor_ic": rows,
        "bull_share_in_sample": _bull_share(is_df),
        "bull_share_oos": _bull_share(oos_df),
        "inverted_factors": [r["factor"] for r in rows if r["sign_flip"]],
    }


# ─────────────────────────────────────────────────────────────────────────────
# WALK-FORWARD SWEEP
# ─────────────────────────────────────────────────────────────────────────────
def run_sweep(*, top_n: int = 10, quick: bool = False, exclude_avoid: bool = True,
              persist: bool = True) -> SweepReport:
    from datetime import datetime
    df = load_dataset()
    grid = _make_grid(quick)

    # Precompute, ONCE per profile: selected basket + per-date mean fwd20.
    base_sel = _selected(df, _BASELINE, top_n, exclude_avoid)
    base_perdate = _perdate_mean(base_sel)

    profiles: list[tuple[dict, pd.DataFrame, pd.Series]] = []
    for w in grid:
        sel = _selected(df, w, top_n, exclude_avoid)
        profiles.append((w, sel, _perdate_mean(sel)))

    years = sorted(df["year"].unique())
    # Need ≥1 prior year to select on; test on every year that has a prior.
    test_years = [y for y in years if any(yy < y for yy in years)]
    # Focus the walk-forward on the genuinely unseen recent years.
    test_years = [y for y in test_years if y >= (years[0] + 2)] or test_years[-2:]

    per_year: list[dict] = []
    wf_edge_parts: list[pd.Series] = []
    wf_sel_parts:  list[pd.DataFrame] = []

    for y in test_years:
        # In-sample selection: pick the profile with the best edge on years < y.
        best_w, best_is_edge = None, -1e9
        for w, sel, perdate in profiles:
            if w == _BASELINE:
                continue
            is_edge = _edge_series(perdate[perdate.index.str[:4].astype(int) < y],
                                   base_perdate[base_perdate.index.str[:4].astype(int) < y])
            m = float(is_edge.mean()) if len(is_edge) else -1e9
            if m > best_is_edge:
                best_is_edge, best_w = m, w

        # OOS measurement of the chosen profile on year y.
        chosen = next(p for p in profiles if p[0] == best_w)
        _, chosen_sel, chosen_perdate = chosen
        oos_mask = chosen_perdate.index.str[:4].astype(int) == y
        oos_edge = _edge_series(chosen_perdate[oos_mask],
                                base_perdate[base_perdate.index.str[:4].astype(int) == y])
        oos_sel = chosen_sel[chosen_sel["year"] == y]
        wf_edge_parts.append(oos_edge)
        wf_sel_parts.append(oos_sel)
        per_year.append({
            "test_year": int(y),
            "chosen_profile": {k: best_w[k] for k in ("rs", "sector_c", "breakout", "trend")},
            "in_sample_edge_pct": round(best_is_edge, 3),
            "oos_edge_pct": round(float(oos_edge.mean()), 3) if len(oos_edge) else None,
            "oos_winrate_pct": _winrate(oos_sel),
            "oos_profit_factor": _profit_factor(oos_sel),
            "n_dates": int(len(oos_edge)),
        })

    wf_edge = pd.concat(wf_edge_parts) if wf_edge_parts else pd.Series(dtype=float)
    wf_sel  = pd.concat(wf_sel_parts) if wf_sel_parts else base_sel.iloc[0:0]
    ci_lo, ci_hi = _bootstrap_ci(wf_edge)
    pooled_edge = round(float(wf_edge.mean()), 3) if len(wf_edge) else 0.0

    # Baseline OOS tradability over the same test years (absolute, not edge).
    base_oos_sel = base_sel[base_sel["year"].isin(test_years)]

    walk_forward = {
        "test_years": [int(y) for y in test_years],
        "pooled_oos_edge_pct": pooled_edge,
        "ci95": [ci_lo, ci_hi],
        "ci_excludes_zero": bool(not np.isnan(ci_lo) and ci_lo > 0),
        "oos_winrate_pct": _winrate(wf_sel),
        "oos_profit_factor": _profit_factor(wf_sel),
        "oos_mean_ret_pct": round(float(wf_sel[_HORIZON].mean()), 3) if len(wf_sel) else None,
        "n_dates": int(len(wf_edge)),
    }
    baseline_oos = {
        "winrate_pct": _winrate(base_oos_sel),
        "profit_factor": _profit_factor(base_oos_sel),
        "mean_ret_pct": round(float(base_oos_sel[_HORIZON].mean()), 3) if len(base_oos_sel) else None,
    }

    # Descriptive grid table: rank profiles by edge on the recent holdout
    # (test_years pooled). This is NOT used to adopt — it exists to SHOW how
    # tempting (and unstable) in-sample-style ranking is.
    grid_rows = []
    holdout = set(test_years)
    for w, sel, perdate in profiles:
        if w == _BASELINE:
            continue
        ho_mask = perdate.index.str[:4].astype(int).isin(holdout)
        edge = _edge_series(perdate[ho_mask],
                            base_perdate[base_perdate.index.str[:4].astype(int).isin(holdout)])
        ho_sel = sel[sel["year"].isin(holdout)]
        grid_rows.append(ProfileResult(
            weights=w, oos_edge=round(float(edge.mean()), 3) if len(edge) else 0.0,
            oos_winrate=_winrate(ho_sel), oos_pf=_profit_factor(ho_sel),
            oos_mean_ret=round(float(ho_sel[_HORIZON].mean()), 3) if len(ho_sel) else 0.0,
            n_dates=int(len(edge))))
    grid_rows.sort(key=lambda r: -r.oos_edge)
    grid_top = [{"profile": r.label(), "weights":
                 {k: r.weights[k] for k in ("rs", "sector_c", "breakout", "trend")},
                 "holdout_edge_pct": r.oos_edge, "winrate_pct": r.oos_winrate,
                 "profit_factor": r.oos_pf} for r in grid_rows[:8]]

    # VERDICT — the gate. Adopt only with a positive WF edge AND CI clear of 0.
    if walk_forward["ci_excludes_zero"] and pooled_edge > 0:
        # The recommendation is the modal walk-forward choice (most-picked profile)
        from collections import Counter
        picks = Counter(tuple(sorted(p["chosen_profile"].items())) for p in per_year)
        rec_items = dict(picks.most_common(1)[0][0])
        recommended = dict(rec_items); recommended.update(_FIXED)
        verdict = (f"ADOPT (walk-forward OOS edge +{pooled_edge:.2f}%, "
                   f"CI [{ci_lo:+.2f},{ci_hi:+.2f}] excludes 0)")
    else:
        recommended = None
        verdict = ("NO DEPLOYABLE PROFILE — keep NEUTRAL baseline "
                   f"(WF OOS edge {pooled_edge:+.2f}%, CI [{ci_lo:+.2f},{ci_hi:+.2f}] "
                   f"includes 0 — not separable from noise; §5 holds)")

    notes = [
        "Walk-forward: each year's profile was chosen using ONLY prior years, then "
        "scored on the unseen year. In-sample edge is shown alongside OOS to expose decay.",
        "Breakout=0 profiles are in the grid (Phase E: Breakout net-negative every year).",
        "Adopting a profile is a manual, separate act (edit config/regime_profiles.yaml) and "
        "must pass a fresh Trust audit first — this module changes no production scoring.",
    ]

    rep = SweepReport(
        verdict=verdict, recommended=recommended, walk_forward=walk_forward,
        baseline_oos=baseline_oos, per_year=per_year, grid_top=grid_top,
        diagnostic_2025=diagnose_oos(df), n_profiles=len(grid),
        generated_at=datetime.now().isoformat(timespec="seconds"), notes=notes)

    if persist:
        try:
            _OUT.parent.mkdir(parents=True, exist_ok=True)
            _OUT.write_text(json.dumps({
                "verdict": rep.verdict, "recommended": rep.recommended,
                "walk_forward": rep.walk_forward, "baseline_oos": rep.baseline_oos,
                "per_year": rep.per_year, "grid_top": rep.grid_top,
                "diagnostic_2025": rep.diagnostic_2025, "n_profiles": rep.n_profiles,
                "generated_at": rep.generated_at, "notes": rep.notes,
            }, indent=2), encoding="utf-8")
            _log.info("Wrote %s", _OUT)
        except OSError as exc:
            _log.warning("Could not persist sweep report: %s", exc)
    return rep


# ─────────────────────────────────────────────────────────────────────────────
# RENDER
# ─────────────────────────────────────────────────────────────────────────────
def render_report(rep: SweepReport) -> None:
    G, R, Y, B, D, RST = ("\033[92m", "\033[91m", "\033[93m", "\033[1m", "\033[2m", "\033[0m")
    print(f"\n  {B}{'═'*76}{RST}")
    print(f"  {B}  WALK-FORWARD WEIGHT SWEEP  ·  {rep.n_profiles} profiles  ·  "
          f"OOS-gated{RST}")
    print(f"  {B}{'═'*76}{RST}")

    # 2025 diagnostic
    dg = rep.diagnostic_2025
    print(f"\n  {B}WHY THE EDGE DROPPED (factor IC: in-sample < {dg['split_year']} "
          f"vs OOS ≥ {dg['split_year']}){RST}")
    print(f"  {D}  {'factor':10s} {'IS IC':>8} {'OOS IC':>8} {'Δ':>8}   flip{RST}")
    for r in dg["factor_ic"]:
        flip = f"{R}SIGN FLIP{RST}" if r["sign_flip"] else f"{D}—{RST}"
        is_ic = "n/a" if r["ic_in_sample"] is None or np.isnan(r["ic_in_sample"]) else f"{r['ic_in_sample']:+.3f}"
        oos_ic = "n/a" if r["ic_oos"] is None or np.isnan(r["ic_oos"]) else f"{r['ic_oos']:+.3f}"
        dlt = "" if r["delta"] is None else f"{r['delta']:+.3f}"
        print(f"  {D}  {r['factor']:10s} {is_ic:>8} {oos_ic:>8} {dlt:>8}{RST}   {flip}")
    if dg["inverted_factors"]:
        print(f"  {R}  Inverted OOS: {', '.join(dg['inverted_factors'])} "
              f"— these factors stopped predicting (Phase H confirmed){RST}")
    print(f"  {D}  BULL share {dg['bull_share_in_sample']} → {dg['bull_share_oos']} "
          f"(market regime mix shifted){RST}")

    # Per-year walk-forward
    print(f"\n  {B}WALK-FORWARD (chose on past years → scored on the unseen year){RST}")
    print(f"  {D}  {'year':>4}  {'chosen RS/Sec/Brk/Trn':24s}  {'IS edge':>8}  "
          f"{'OOS edge':>9}  {'win%':>5}  {'PF':>5}{RST}")
    for p in rep.per_year:
        cp = p["chosen_profile"]
        prof = f"{cp['rs']}/{cp['sector_c']}/{cp['breakout']}/{cp['trend']}"
        oe = p["oos_edge_pct"]
        oc = G if (oe or 0) > 0 else R
        oe_s = "n/a" if oe is None else f"{oe:+.2f}%"
        print(f"  {D}  {p['test_year']:>4}  {prof:24s}  {p['in_sample_edge_pct']:+7.2f}%  "
              f"{oc}{oe_s:>9}{RST}{D}  {p['oos_winrate_pct']:>4.0f}%  "
              f"{p['oos_profit_factor']:>5}{RST}")

    wf = rep.walk_forward
    bo = rep.baseline_oos
    print(f"\n  {B}POOLED OUT-OF-SAMPLE{RST}")
    ec = G if wf["pooled_oos_edge_pct"] > 0 else R
    print(f"  {D}  swept edge vs baseline: {ec}{wf['pooled_oos_edge_pct']:+.2f}%{RST}"
          f"{D}  CI95 [{wf['ci95'][0]:+.2f}, {wf['ci95'][1]:+.2f}]  "
          f"(n={wf['n_dates']} dates){RST}")
    print(f"  {D}  swept basket:    win {wf['oos_winrate_pct']:.0f}%  ·  "
          f"PF {wf['oos_profit_factor']}  ·  mean fwd20 {wf['oos_mean_ret_pct']}%{RST}")
    print(f"  {D}  NEUTRAL baseline: win {bo['winrate_pct']:.0f}%  ·  "
          f"PF {bo['profit_factor']}  ·  mean fwd20 {bo['mean_ret_pct']}%{RST}")

    # Descriptive grid (the tempting-but-unadopted leaderboard)
    print(f"\n  {B}GRID LEADERBOARD (recent holdout edge — DESCRIPTIVE, not adopted){RST}")
    print(f"  {D}  {'RS/Sec/Brk/Trn':18s}  {'edge':>7}  {'win%':>5}  {'PF':>5}{RST}")
    for g in rep.grid_top:
        w = g["weights"]
        prof = f"{w['rs']}/{w['sector_c']}/{w['breakout']}/{w['trend']}"
        print(f"  {D}  {prof:18s}  {g['holdout_edge_pct']:+6.2f}%  "
              f"{g['winrate_pct']:>4.0f}%  {g['profit_factor']:>5}{RST}")

    # Verdict
    adopt = rep.recommended is not None
    vc = G if adopt else Y
    print(f"\n  {B}{'─'*76}{RST}")
    print(f"  {vc}{B}  VERDICT: {rep.verdict}{RST}")
    if adopt:
        rc = rep.recommended
        print(f"  {G}  → RS {rc['rs']} · Sector {rc['sector_c']} · Breakout "
              f"{rc['breakout']} · Trend {rc['trend']}  (liquidity/atr/freshness fixed){RST}")
        print(f"  {Y}  Adopt MANUALLY in config/regime_profiles.yaml, then re-run the "
              f"Trust audit BEFORE trusting it live.{RST}")
    else:
        print(f"  {Y}  The data does not support changing weights. Keeping NEUTRAL honors "
              f"§5 (validation precedes modification).{RST}")
    for n in rep.notes:
        print(f"  {D}  • {n}{RST}")
    print(f"  {B}{'─'*76}{RST}\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Walk-forward OOS weight sweep + diagnostic")
    ap.add_argument("--quick", action="store_true", help="coarse grid (fast)")
    ap.add_argument("--top-n", type=int, default=10, dest="top_n",
                    help="selection basket size per screen date (default 10)")
    ap.add_argument("--include-avoid", action="store_true",
                    help="include AVOID-class names in the selectable pool")
    args = ap.parse_args(argv)
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8")  # box glyphs on Windows
    except Exception:
        pass
    try:
        rep = run_sweep(top_n=args.top_n, quick=args.quick,
                        exclude_avoid=not args.include_avoid, persist=True)
    except (FileNotFoundError, ValueError) as exc:
        _log.error("%s", exc)
        return 1
    render_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
