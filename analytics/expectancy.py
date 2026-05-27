"""
analytics/expectancy.py — Statistical Edge Measurement Engine
=============================================================
Computes every metric that tells you WHETHER your process has edge.

Key outputs:
  - Expectancy (the single most important number)
  - Win rate, avg win R, avg loss R
  - Grade-wise and regime-wise breakdown
  - MFE/MAE analysis (stop and target quality)
  - Drawdown streaks
  - Minimum sample size warnings

WHY expectancy, not win rate?
    A system with 35% win rate and avg 3R wins vs -1R losses has:
    Expectancy = (0.35 × 3) + (0.65 × -1) = 1.05 − 0.65 = +0.40R per trade.
    That's profitable. A 60% win rate with avg 0.8R wins vs -1R losses:
    Expectancy = (0.60 × 0.8) + (0.40 × -1) = 0.48 − 0.40 = +0.08R per trade.
    Barely profitable and fragile. Expectancy is the truth.

WHY minimum sample warnings?
    Any system can look great on 15 trades. Below ~30 resolved trades,
    variance dominates signal. We flag this rather than hide it.
"""

import pandas as pd
import numpy as np
from typing import Optional


# ── Constants ─────────────────────────────────────────────────────────────────
MIN_RELIABLE_SAMPLE = 30   # Below this, warn the user loudly
MIN_ANALYSIS_SAMPLE = 10   # Below this, refuse to compute metrics


def _warn_sample(n: int, context: str = "") -> None:
    if n < MIN_ANALYSIS_SAMPLE:
        print(f"  ⚠  INSUFFICIENT DATA: {n} trades{' (' + context + ')' if context else ''}. "
              f"Need at least {MIN_ANALYSIS_SAMPLE} to compute metrics.")
    elif n < MIN_RELIABLE_SAMPLE:
        print(f"  ⚠  LOW SAMPLE WARNING: {n} trades{' (' + context + ')' if context else ''}. "
              f"Results have high variance. Need {MIN_RELIABLE_SAMPLE}+ for reliability.")


# ═══════════════════════════════════════════════════
# CORE EXPECTANCY METRICS
# ═══════════════════════════════════════════════════
def compute_expectancy_stats(resolved_trades: pd.DataFrame) -> Optional[dict]:
    """
    Computes the core expectancy statistics from resolved trades.

    Input DataFrame must have:
      - r_multiple   : float (actual R outcome — negative for losses)
      - exit_reason  : str   (T1_HIT / T2_HIT / STOP_HIT / MANUAL)

    Returns dict with all stats, or None if insufficient data.
    """
    df = resolved_trades.copy()
    df = df[df["r_multiple"].notna()]

    n = len(df)
    _warn_sample(n)

    if n < MIN_ANALYSIS_SAMPLE:
        return None

    wins  = df[df["r_multiple"] > 0]
    losses = df[df["r_multiple"] <= 0]

    win_rate  = len(wins) / n
    avg_win_r = float(wins["r_multiple"].mean())   if len(wins)   > 0 else 0.0
    avg_loss_r = float(losses["r_multiple"].mean()) if len(losses) > 0 else 0.0

    # Expectancy: the average R per trade across all trades
    # Positive expectancy = the system makes money in the long run
    expectancy = float(df["r_multiple"].mean())

    # Expectancy per trade in R-terms (Kelly-like insight)
    # Interpretation: for every ₹1 risked, you expect to make ₹expectancy
    profit_factor = (
        abs(wins["r_multiple"].sum()) / abs(losses["r_multiple"].sum())
        if len(losses) > 0 and losses["r_multiple"].sum() != 0 else float("inf")
    )

    # Consecutive loss streak (important for drawdown psychology)
    r_vals         = df["r_multiple"].values
    max_loss_streak = _max_consecutive_losses(r_vals)
    max_win_streak  = _max_consecutive_wins(r_vals)

    # Stop accuracy: what % of stops were "good" (trade went on to reach T1 after stop)
    # If stop_hit_pct is very high AND win_rate is low: stops may be too tight
    stop_hit_pct = len(df[df["exit_reason"] == "STOP_HIT"]) / n * 100
    t1_hit_pct   = len(df[df["exit_reason"] == "T1_HIT"])  / n * 100
    t2_hit_pct   = len(df[df["exit_reason"] == "T2_HIT"])  / n * 100

    avg_days_held = float(df["days_held"].mean()) if "days_held" in df.columns else None

    return {
        "n_trades":         n,
        "n_wins":           len(wins),
        "n_losses":         len(losses),
        "win_rate":         round(win_rate, 3),
        "win_rate_pct":     round(win_rate * 100, 1),
        "avg_win_r":        round(avg_win_r, 3),
        "avg_loss_r":       round(avg_loss_r, 3),
        "expectancy_r":     round(expectancy, 3),
        "profit_factor":    round(profit_factor, 2),
        "max_loss_streak":  max_loss_streak,
        "max_win_streak":   max_win_streak,
        "stop_hit_pct":     round(stop_hit_pct, 1),
        "t1_hit_pct":       round(t1_hit_pct, 1),
        "t2_hit_pct":       round(t2_hit_pct, 1),
        "avg_days_held":    round(avg_days_held, 1) if avg_days_held else None,
        "total_r":          round(float(df["r_multiple"].sum()), 2),
    }


def _max_consecutive_losses(r_vals: np.ndarray) -> int:
    max_streak = current = 0
    for r in r_vals:
        if r <= 0:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak


def _max_consecutive_wins(r_vals: np.ndarray) -> int:
    max_streak = current = 0
    for r in r_vals:
        if r > 0:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak


# ═══════════════════════════════════════════════════
# SEGMENT ANALYSIS: GRADE AND REGIME
# ═══════════════════════════════════════════════════
def compute_segment_analysis(df: pd.DataFrame,
                              segment_col: str) -> pd.DataFrame:
    """
    Computes expectancy stats for each unique value in segment_col.
    Used for grade-wise and regime-wise breakdowns.

    WHY: If A+ setups have 0.80R expectancy and B setups have -0.20R,
    you should filter out B grades entirely. This is how you improve
    the system empirically over time — by measuring which categories
    actually work.
    """
    resolved = df[df["r_multiple"].notna()].copy()

    if resolved.empty or segment_col not in resolved.columns:
        return pd.DataFrame()

    rows = []
    for segment_val in resolved[segment_col].unique():
        subset = resolved[resolved[segment_col] == segment_val]
        n = len(subset)
        if n < 3:
            continue   # Not enough to say anything

        wins  = subset[subset["r_multiple"] > 0]
        exp_r = float(subset["r_multiple"].mean())
        wr    = len(wins) / n * 100

        rows.append({
            segment_col:     segment_val,
            "n_trades":      n,
            "win_rate_pct":  round(wr, 1),
            "expectancy_r":  round(exp_r, 3),
            "avg_win_r":     round(float(wins["r_multiple"].mean()), 2) if len(wins) > 0 else 0,
            "avg_loss_r":    round(float(subset[subset["r_multiple"] <= 0]["r_multiple"].mean()), 2)
                             if len(subset[subset["r_multiple"] <= 0]) > 0 else 0,
            "total_r":       round(float(subset["r_multiple"].sum()), 2),
        })

    if not rows:
        return pd.DataFrame()

    result = pd.DataFrame(rows).sort_values("expectancy_r", ascending=False)
    return result.reset_index(drop=True)


# ═══════════════════════════════════════════════════
# MFE / MAE QUALITY ANALYSIS
# ═══════════════════════════════════════════════════
def compute_mfe_mae_analysis(df: pd.DataFrame) -> dict:
    """
    Analyses stop and target placement quality using MFE/MAE data.

    Key diagnostics:

    STOP QUALITY:
      avg MAE on WINNING trades:
        If this is larger than your stop distance, stops are too tight.
        Winners would have been stopped out in a different configuration.

    TARGET QUALITY:
      avg MFE on LOSING trades:
        If this is large, the trade moved in your favour before stopping out.
        Consider trailing stops or earlier partial profit taking.

    These are the two most actionable metrics for system improvement.
    """
    resolved = df[df["r_multiple"].notna() & df["mfe_pct"].notna()].copy()

    if resolved.empty:
        return {}

    wins   = resolved[resolved["r_multiple"] > 0]
    losses = resolved[resolved["r_multiple"] <= 0]

    return {
        # How far winners moved against us before winning
        "avg_mae_winners":  round(float(wins["mae_pct"].mean()), 2) if len(wins) > 0 else None,
        # How far losers moved in our favour before losing
        "avg_mfe_losers":   round(float(losses["mfe_pct"].mean()), 2) if len(losses) > 0 else None,
        # Best-case: if you'd held to MFE every time
        "avg_mfe_all":      round(float(resolved["mfe_pct"].mean()), 2),
        "avg_mae_all":      round(float(resolved["mae_pct"].mean()), 2),
        # Edge ratio: MFE/MAE — >1.0 means trades move more in your favour than against
        "edge_ratio":       round(
            float(resolved["mfe_pct"].mean()) / abs(float(resolved["mae_pct"].mean()))
            if resolved["mae_pct"].mean() != 0 else 0.0, 2
        ),
    }


# ═══════════════════════════════════════════════════
# EQUITY CURVE SIMULATION
# ═══════════════════════════════════════════════════
def simulate_equity_curve(
    resolved_trades: pd.DataFrame,
    initial_capital: float = 500_000,
    risk_per_trade:  float = 0.01,
) -> dict:
    """
    Simulates account growth using actual resolved R-multiples.

    WHY simulate, not just sum?
        Fixed fractional sizing is multiplicative, not additive.
        A 1% risk on ₹5,00,000 = ₹5,000.
        After a -1R loss: capital = ₹4,95,000. Next 1% = ₹4,950.
        After a +2R win from ₹4,95,000: capital = ₹5,04,900.
        This compounds — both up and down.

    Returns equity series, drawdown stats, and final account value.

    IMPORTANT: Trades are assumed sequential. In reality you may hold
    multiple positions. This simulation is conservative and illustrative —
    it shows long-run expectancy, not a precise P&L.
    """
    df = resolved_trades.copy()
    df = df[df["r_multiple"].notna()].reset_index(drop=True)

    if df.empty:
        return {}

    capital    = initial_capital
    equity     = [capital]
    peak       = capital
    drawdowns  = []
    current_dd = 0.0

    for _, row in df.iterrows():
        risk_inr  = capital * risk_per_trade
        pnl       = risk_inr * float(row["r_multiple"])
        capital   = max(0, capital + pnl)
        equity.append(capital)

        # Track drawdown
        if capital > peak:
            peak       = capital
            current_dd = 0.0
        else:
            dd = (peak - capital) / peak * 100
            current_dd = max(current_dd, dd)
            drawdowns.append(dd)

    max_drawdown_pct = round(max(drawdowns), 2) if drawdowns else 0.0
    final_capital    = round(capital, 2)
    total_return_pct = round((final_capital - initial_capital) / initial_capital * 100, 2)
    cagr             = None  # Requires date info — computed separately if available

    # Time under water: % of equity curve below the prior peak
    equity_series   = pd.Series(equity)
    rolling_max     = equity_series.cummax()
    underwater_pct  = round(((equity_series < rolling_max).sum() / len(equity_series)) * 100, 1)

    return {
        "initial_capital":   initial_capital,
        "final_capital":     final_capital,
        "total_return_pct":  total_return_pct,
        "max_drawdown_pct":  max_drawdown_pct,
        "underwater_pct":    underwater_pct,   # % of time not at new highs
        "n_trades":          len(df),
        "equity_series":     equity,           # list of floats, for charting
    }


# ═══════════════════════════════════════════════════
# SCORE CORRELATION ANALYSIS
# ═══════════════════════════════════════════════════
def compute_score_correlation(df: pd.DataFrame) -> dict:
    """
    Checks whether the scanner's score actually predicts outcomes.

    WHY this matters:
        If higher-scored setups systematically produce better R-multiples,
        the scoring weights are working. If there's no correlation, the
        scoring engine needs recalibration.

    Also correlates individual metrics with outcomes to identify which
    filters are actually predictive vs just noise.
    """
    resolved = df[df["r_multiple"].notna()].copy()

    if len(resolved) < MIN_ANALYSIS_SAMPLE:
        return {}

    result = {}

    # Score → R correlation
    if "score" in resolved.columns:
        corr = resolved["score"].corr(resolved["r_multiple"])
        result["score_vs_r_corr"] = round(corr, 3)
        result["score_predictive"] = abs(corr) > 0.15   # Weak but useful threshold

    # Individual metric correlations
    metric_cols = ["relative_strength", "range_pct", "volume_ratio",
                   "atr_pct", "trend_score"]
    metric_corrs = {}
    for col in metric_cols:
        if col in resolved.columns and resolved[col].notna().sum() > MIN_ANALYSIS_SAMPLE:
            c = resolved[col].corr(resolved["r_multiple"])
            metric_corrs[col] = round(c, 3)

    result["metric_correlations"] = metric_corrs

    # Rank metrics by absolute correlation (most → least predictive)
    if metric_corrs:
        ranked = sorted(metric_corrs.items(), key=lambda x: abs(x[1]), reverse=True)
        result["ranked_by_predictiveness"] = ranked

    return result


# ═══════════════════════════════════════════════════
# EDGE INTELLIGENCE — BUCKETED EXPECTANCY
# ═══════════════════════════════════════════════════
# Statistical slicing of the trade log to find WHERE the edge actually lives.
# No predictions. No ML. Just honest measurement of conditional expectancy.

ATR_BUCKETS = [
    ("LOW   (<2.0%)",   0.0,  2.0),
    ("MID   (2-3.5%)",  2.0,  3.5),
    ("HIGH  (3.5-5%)",  3.5,  5.0),
    ("XHIGH (>5%)",     5.0,  9999.0),
]

RS_BUCKETS = [
    ("WEAK     (<0%)",       -9999.0,  0.0),
    ("NEUTRAL  (0-5%)",       0.0,     5.0),
    ("STRONG   (5-15%)",      5.0,    15.0),
    ("V.STRONG (>15%)",      15.0,  9999.0),
]


def _bucket_label(value: float, buckets: list) -> str | None:
    """Maps a numeric value to its bucket label, or None if unmappable."""
    if pd.isna(value):
        return None
    for label, lo, hi in buckets:
        if lo <= value < hi:
            return label
    return None


def _sector_from_ticker(ticker: str, config: dict) -> str:
    return config.get("sector_map", {}).get(str(ticker), "OTHER")


def compute_atr_bucket_analysis(df: pd.DataFrame) -> pd.DataFrame:
    """Expectancy by entry-bar ATR volatility bucket."""
    if "atr_pct" not in df.columns:
        return pd.DataFrame()
    work = df.copy()
    work["atr_bucket"] = work["atr_pct"].apply(lambda v: _bucket_label(v, ATR_BUCKETS))
    work = work[work["atr_bucket"].notna()]
    return _ordered_segment(work, "atr_bucket", [b[0] for b in ATR_BUCKETS])


def compute_rs_bucket_analysis(df: pd.DataFrame) -> pd.DataFrame:
    """Expectancy by entry-bar relative-strength bucket."""
    if "relative_strength" not in df.columns:
        return pd.DataFrame()
    work = df.copy()
    work["rs_bucket"] = work["relative_strength"].apply(lambda v: _bucket_label(v, RS_BUCKETS))
    work = work[work["rs_bucket"].notna()]
    return _ordered_segment(work, "rs_bucket", [b[0] for b in RS_BUCKETS])


def compute_sector_analysis(df: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Expectancy by mapped sector. Highlights concentration of edge."""
    if "ticker" not in df.columns:
        return pd.DataFrame()
    work = df.copy()
    work["sector"] = work["ticker"].apply(lambda t: _sector_from_ticker(t, config))
    return compute_segment_analysis(work, "sector")


def _ordered_segment(df: pd.DataFrame, col: str, order: list) -> pd.DataFrame:
    """Like compute_segment_analysis but preserves a fixed category order."""
    seg = compute_segment_analysis(df, col)
    if seg.empty:
        return seg
    rank = {v: i for i, v in enumerate(order)}
    seg = seg.sort_values(col, key=lambda s: s.map(lambda x: rank.get(x, 99)))
    return seg.reset_index(drop=True)


# ═══════════════════════════════════════════════════
# MFE / MAE DISTRIBUTIONS
# ═══════════════════════════════════════════════════
def compute_mfe_mae_distribution(df: pd.DataFrame) -> dict:
    """
    Returns percentile distributions for MFE and MAE, plus a coarse
    histogram suitable for ASCII rendering.

    WHY distributions, not just averages?
        A mean MFE of 3% can come from (3,3,3,3) or (0,0,0,12). Those are
        radically different trading conditions. Percentiles + histogram
        expose which one you actually have.
    """
    resolved = df[df["r_multiple"].notna() & df["mfe_pct"].notna() & df["mae_pct"].notna()].copy()
    if len(resolved) < MIN_ANALYSIS_SAMPLE:
        return {}

    def _pcts(s: pd.Series) -> dict:
        return {
            "p10":    round(float(s.quantile(0.10)), 2),
            "p25":    round(float(s.quantile(0.25)), 2),
            "median": round(float(s.median()),       2),
            "p75":    round(float(s.quantile(0.75)), 2),
            "p90":    round(float(s.quantile(0.90)), 2),
            "max":    round(float(s.max()),          2),
            "min":    round(float(s.min()),          2),
        }

    # Histogram bins (in pct points)
    mfe_bins = [0, 1, 2, 3, 5, 8, 12, 20, 100]
    mae_bins = [-100, -12, -8, -5, -3, -2, -1, -0.5, 0]

    mfe_counts = pd.cut(resolved["mfe_pct"], bins=mfe_bins, include_lowest=True).value_counts().sort_index()
    mae_counts = pd.cut(resolved["mae_pct"], bins=mae_bins, include_lowest=True).value_counts().sort_index()

    return {
        "n":                len(resolved),
        "mfe_percentiles":  _pcts(resolved["mfe_pct"]),
        "mae_percentiles":  _pcts(resolved["mae_pct"]),
        "mfe_histogram":    [(str(idx), int(cnt)) for idx, cnt in mfe_counts.items()],
        "mae_histogram":    [(str(idx), int(cnt)) for idx, cnt in mae_counts.items()],
    }


# ═══════════════════════════════════════════════════
# ROLLING EXPECTANCY
# ═══════════════════════════════════════════════════
def compute_rolling_expectancy(df: pd.DataFrame, window: int = 20) -> dict:
    """
    Walks the resolved trade log chronologically and reports a rolling
    window expectancy. Flags whether the edge is stable, decaying, or
    improving over time.

    WHY: A single all-time expectancy number hides regime drift. If your
    last 20 trades show -0.3R but lifetime is +0.4R, the system has
    likely degraded and needs review BEFORE the next entry.
    """
    resolved = df[df["r_multiple"].notna()].copy()
    if len(resolved) < window:
        return {}

    resolved = resolved.sort_values("entry_date", na_position="last").reset_index(drop=True)
    r = resolved["r_multiple"].astype(float)

    rolling = r.rolling(window=window, min_periods=window).mean()
    cumulative = r.expanding(min_periods=1).mean()

    recent = round(float(rolling.dropna().iloc[-1]), 3) if rolling.notna().any() else None
    lifetime = round(float(r.mean()), 3)

    if recent is None:
        trend = "INSUFFICIENT"
    else:
        delta = recent - lifetime
        if   delta >  0.15: trend = "IMPROVING"
        elif delta < -0.15: trend = "DECAYING"
        else:               trend = "STABLE"

    return {
        "window":          window,
        "n_trades":        len(resolved),
        "lifetime_exp_r":  lifetime,
        "recent_exp_r":    recent,
        "trend":           trend,
        "rolling_series":  [None if pd.isna(v) else round(float(v), 3) for v in rolling],
        "cumulative_series": [round(float(v), 3) for v in cumulative],
    }


# ═══════════════════════════════════════════════════
# ROLLING DRAWDOWN
# ═══════════════════════════════════════════════════
def compute_rolling_drawdown(equity_series: list) -> dict:
    """
    Derives the drawdown curve from a simulated equity series.

    Returns peak DD, current DD, longest underwater run (in trades),
    and the full DD series for sparkline rendering.
    """
    if not equity_series or len(equity_series) < 2:
        return {}

    eq = pd.Series(equity_series, dtype=float)
    peak = eq.cummax()
    dd_pct = (eq - peak) / peak * 100   # always <= 0

    # Longest underwater streak (consecutive bars below peak)
    underwater = (dd_pct < 0).astype(int).values
    longest = current = 0
    for v in underwater:
        if v:
            current += 1
            longest = max(longest, current)
        else:
            current = 0

    return {
        "max_dd_pct":         round(float(dd_pct.min()), 2),     # most negative
        "current_dd_pct":     round(float(dd_pct.iloc[-1]), 2),
        "longest_underwater": int(longest),
        "underwater_pct":     round(float((dd_pct < 0).sum() / len(dd_pct) * 100), 1),
        "dd_series":          [round(float(v), 2) for v in dd_pct],
    }


# ═══════════════════════════════════════════════════
# TRADE DURATION ANALYSIS
# ═══════════════════════════════════════════════════
def compute_duration_analysis(df: pd.DataFrame) -> dict:
    """
    How long do trades take to resolve? Split by outcome.

    WHY: Winners that take 30 days vs losers that exit in 3 days reveals
    a healthy 'cut quick, ride long' profile. The inverse (winners exit
    fast, losers linger) signals premature profit-taking and poor stops.
    """
    resolved = df[df["r_multiple"].notna() & df["days_held"].notna()].copy()
    if len(resolved) < MIN_ANALYSIS_SAMPLE:
        return {}

    resolved["days_held"] = pd.to_numeric(resolved["days_held"], errors="coerce")
    resolved = resolved[resolved["days_held"].notna()]

    wins   = resolved[resolved["r_multiple"] > 0]["days_held"]
    losses = resolved[resolved["r_multiple"] <= 0]["days_held"]
    all_d  = resolved["days_held"]

    def _stats(s: pd.Series) -> dict:
        if s.empty:
            return {"n": 0}
        return {
            "n":      int(len(s)),
            "mean":   round(float(s.mean()),   1),
            "median": round(float(s.median()), 1),
            "p90":    round(float(s.quantile(0.90)), 1),
            "max":    int(s.max()),
        }

    # Bucket distribution
    bins = [0, 3, 7, 14, 30, 60, 9999]
    labels = ["1-3d", "4-7d", "8-14d", "15-30d", "31-60d", "60d+"]
    buckets = pd.cut(all_d, bins=bins, labels=labels, include_lowest=True)
    bucket_counts = buckets.value_counts().reindex(labels, fill_value=0)

    return {
        "all":      _stats(all_d),
        "winners":  _stats(wins),
        "losers":   _stats(losses),
        "buckets":  [(lbl, int(cnt)) for lbl, cnt in bucket_counts.items()],
    }

