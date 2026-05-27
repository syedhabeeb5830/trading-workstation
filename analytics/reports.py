"""
analytics/reports.py — Terminal Analytics Report Generator
===========================================================
Assembles all analytics modules into clean, readable terminal reports.

Three report types:
  1. print_expectancy_report()   — core edge measurement (run weekly)
  2. print_segment_report()      — grade/regime breakdown (run monthly)
  3. print_equity_report()       — simulated account performance
  4. print_full_report()         — all three in sequence

Design: every report function is independently callable.
If you only want to see the segment breakdown, just call that one.

Output is intentionally terminal-first. No matplotlib yet — the numbers
tell the story clearly. Charts are a Phase 4 addition.
"""

import pandas as pd
from datetime import datetime
from analytics.outcome_tracker import load_trade_log
from analytics.scan_logger     import load_scan_log, get_scan_summary
from analytics.expectancy import (
    compute_expectancy_stats,
    compute_segment_analysis,
    compute_mfe_mae_analysis,
    simulate_equity_curve,
    compute_score_correlation,
    compute_atr_bucket_analysis,
    compute_rs_bucket_analysis,
    compute_sector_analysis,
    compute_mfe_mae_distribution,
    compute_rolling_expectancy,
    compute_rolling_drawdown,
    compute_duration_analysis,
    MIN_RELIABLE_SAMPLE,
    MIN_ANALYSIS_SAMPLE,
)

try:
    from config.config import CONFIG
except ModuleNotFoundError:
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from config.config import CONFIG


# ── Formatting helpers ────────────────────────────────────────────────────────
def _section(title: str, width: int = 70) -> None:
    print("\n" + "─" * width)
    print(f"  {title}")
    print("─" * width)


def _grade_colour(grade: str) -> str:
    return {"A+": "★", "A": "◆", "B": "◇", "AVOID": "✗"}.get(grade, " ")


def _expectancy_verdict(exp_r: float, n: int) -> str:
    if n < MIN_ANALYSIS_SAMPLE:
        return "⚠  INSUFFICIENT DATA"
    if n < MIN_RELIABLE_SAMPLE:
        suffix = " (LOW SAMPLE — INTERPRET CAUTIOUSLY)"
    else:
        suffix = ""

    if exp_r > 0.40:
        return f"🟢 STRONG POSITIVE EDGE{suffix}"
    elif exp_r > 0.15:
        return f"🟡 MODEST POSITIVE EDGE{suffix}"
    elif exp_r > 0:
        return f"🟡 MARGINAL EDGE — WATCH{suffix}"
    elif exp_r > -0.20:
        return f"🔴 MARGINAL NEGATIVE — REVIEW FILTERS{suffix}"
    else:
        return f"🔴 NEGATIVE EXPECTANCY — STOP TRADING THIS SYSTEM{suffix}"


# ═══════════════════════════════════════════════════
# REPORT 1 — CORE EXPECTANCY
# ═══════════════════════════════════════════════════
def print_expectancy_report(journal_dir: str = "journal") -> None:
    """
    The most important report. Run this weekly.
    Tells you: does this system have positive expectancy?
    """
    df     = load_trade_log(journal_dir)
    now_str = datetime.now().strftime("%d %b %Y %H:%M")

    print("\n" + "═" * 70)
    print(f"  TRADE ANALYTICS  |  {now_str}")
    print(f"  Capital: ₹{CONFIG['account_capital']:,.0f}  |  "
          f"Risk/trade: {CONFIG['max_risk_per_trade']*100:.1f}%")
    print("═" * 70)

    # ── Database health ─────────────────────────────────────────────────────
    _section("DATABASE HEALTH")
    total    = len(df)
    resolved = df[df["r_multiple"].notna()]
    open_tr  = df[df["exit_reason"].astype(str) == "OPEN"]

    print(f"  Total trades logged  : {total}")
    print(f"  Resolved             : {len(resolved)}")
    print(f"  Open / pending       : {len(open_tr)}")

    if total == 0:
        print("\n  No trades logged yet. Use record_trade_entry() to start tracking.")
        print("═" * 70 + "\n")
        return

    # ── Core stats ──────────────────────────────────────────────────────────
    _section("CORE EXPECTANCY STATS")
    stats = compute_expectancy_stats(resolved)

    if stats is None:
        print(f"  Need at least {MIN_ANALYSIS_SAMPLE} resolved trades for analysis.")
        print("═" * 70 + "\n")
        return

    n  = stats["n_trades"]
    wr = stats["win_rate_pct"]
    ex = stats["expectancy_r"]

    print(f"  Sample size          : {n} trades")
    print(f"  Win rate             : {wr:.1f}%  ({stats['n_wins']}W / {stats['n_losses']}L)")
    print(f"  Avg win              : +{stats['avg_win_r']:.2f}R")
    print(f"  Avg loss             : {stats['avg_loss_r']:.2f}R")
    print(f"  Expectancy           : {ex:+.3f}R per trade")
    print(f"  Profit factor        : {stats['profit_factor']:.2f}x")
    print(f"  Total R accumulated  : {stats['total_r']:+.1f}R")
    if stats["avg_days_held"]:
        print(f"  Avg holding period   : {stats['avg_days_held']:.1f} days")

    print()
    print(f"  Verdict: {_expectancy_verdict(ex, n)}")

    # ── Exit breakdown ───────────────────────────────────────────────────────
    _section("EXIT BREAKDOWN")
    print(f"  Stop hit (−1R)       : {stats['stop_hit_pct']:.1f}%")
    print(f"  T1 hit (+{CONFIG['target_r_levels'][0]:.0f}R)          : {stats['t1_hit_pct']:.1f}%")
    print(f"  T2 hit (+{CONFIG['target_r_levels'][1]:.0f}R)          : {stats['t2_hit_pct']:.1f}%")

    # ── Streak analysis ─────────────────────────────────────────────────────
    _section("STREAK ANALYSIS  (for drawdown psychology)")
    print(f"  Max consecutive losses : {stats['max_loss_streak']}")
    print(f"  Max consecutive wins   : {stats['max_win_streak']}")

    req_win_rate = 1 / (1 + stats["avg_win_r"]) if stats["avg_win_r"] > 0 else 0
    print(f"\n  Breakeven win rate     : {req_win_rate*100:.1f}%  (need this to cover losses)")
    print(f"  Current win rate       : {wr:.1f}%  "
          f"({'ABOVE' if wr/100 > req_win_rate else 'BELOW'} breakeven)")

    # ── MFE / MAE quality ───────────────────────────────────────────────────
    mfe_mae = compute_mfe_mae_analysis(resolved)
    if mfe_mae:
        _section("STOP & TARGET QUALITY  (MFE/MAE)")
        print(f"  Avg MFE (all trades)   : +{mfe_mae['avg_mfe_all']:.2f}%  "
              "(how far trades moved in your favour)")
        print(f"  Avg MAE (all trades)   : {mfe_mae['avg_mae_all']:.2f}%  "
              "(how far they moved against you)")
        print(f"  Edge ratio (MFE/MAE)   : {mfe_mae['edge_ratio']:.2f}x  "
              "(>1.0 = more upside than downside movement)")
        print()
        if mfe_mae.get("avg_mae_winners") is not None:
            print(f"  Avg MAE on WINNERS     : {mfe_mae['avg_mae_winners']:.2f}%  "
                  "(if > stop%, winners are getting stopped prematurely)")
        if mfe_mae.get("avg_mfe_losers") is not None:
            print(f"  Avg MFE on LOSERS      : +{mfe_mae['avg_mfe_losers']:.2f}%  "
                  "(if large, losers are going green before reversing)")

        # Diagnostics
        if mfe_mae.get("avg_mae_winners"):
            avg_stop_pct = resolved["risk_pct"].mean() if "risk_pct" in resolved.columns else None
            if avg_stop_pct and abs(mfe_mae["avg_mae_winners"]) > avg_stop_pct * 0.8:
                print("\n  ⚠  STOP TIGHTNESS WARNING: winning trades are moving close to stop")
                print("     before recovering. Consider widening stops by 0.5× ATR.")

    print("\n" + "═" * 70 + "\n")


# ═══════════════════════════════════════════════════
# REPORT 2 — SEGMENT BREAKDOWN
# ═══════════════════════════════════════════════════
def print_segment_report(journal_dir: str = "journal") -> None:
    """
    Grade-wise and regime-wise performance breakdown.
    Tells you which categories of setups are actually worth trading.
    Run monthly once you have 30+ trades.
    """
    df       = load_trade_log(journal_dir)
    resolved = df[df["r_multiple"].notna()].copy()

    print("\n" + "═" * 70)
    print("  SEGMENT ANALYSIS")
    print("═" * 70)

    if len(resolved) < MIN_ANALYSIS_SAMPLE:
        print(f"  Need {MIN_ANALYSIS_SAMPLE}+ resolved trades. Currently: {len(resolved)}")
        print("═" * 70 + "\n")
        return

    # ── By Grade ────────────────────────────────────────────────────────────
    _section("EXPECTANCY BY TRADE GRADE")
    grade_df = compute_segment_analysis(resolved, "grade")

    if not grade_df.empty:
        grade_order = {"A+": 0, "A": 1, "B": 2, "AVOID": 3}
        grade_df = grade_df.sort_values(
            "grade",
            key=lambda s: s.map(lambda x: grade_order.get(x, 9))
        )
        hdr = f"  {'GRADE':<8} {'N':>5} {'WIN%':>7} {'EXP(R)':>8} {'AVG_W':>7} {'AVG_L':>7} {'TOTAL_R':>9}"
        print(hdr)
        print("  " + "─" * 57)
        for _, row in grade_df.iterrows():
            icon  = _grade_colour(row["grade"])
            exp_r = row["expectancy_r"]
            flag  = " ← REMOVE" if exp_r < -0.10 and row["n_trades"] >= 5 else ""
            print(
                f"  {icon}{row['grade']:<7} {int(row['n_trades']):>5} "
                f"{row['win_rate_pct']:>6.1f}% "
                f"{exp_r:>+8.3f}R "
                f"{row['avg_win_r']:>+6.2f}R "
                f"{row['avg_loss_r']:>+6.2f}R "
                f"{row['total_r']:>+8.1f}R{flag}"
            )
    else:
        print("  Not enough grade diversity in resolved trades.")

    # ── By Regime ────────────────────────────────────────────────────────────
    _section("EXPECTANCY BY MARKET REGIME")
    regime_df = compute_segment_analysis(resolved, "regime")

    if not regime_df.empty:
        regime_order = {"BULL": 0, "NEUTRAL": 1, "BEAR": 2}
        regime_df = regime_df.sort_values(
            "regime",
            key=lambda s: s.map(lambda x: regime_order.get(x, 9))
        )
        hdr = f"  {'REGIME':<10} {'N':>5} {'WIN%':>7} {'EXP(R)':>8} {'AVG_W':>7} {'AVG_L':>7} {'TOTAL_R':>9}"
        print(hdr)
        print("  " + "─" * 57)
        for _, row in regime_df.iterrows():
            icon  = {"BULL": "🟢", "NEUTRAL": "🟡", "BEAR": "🔴"}.get(row["regime"], " ")
            exp_r = row["expectancy_r"]
            flag  = " ← AVOID TRADING" if exp_r < -0.10 and row["n_trades"] >= 5 else ""
            print(
                f"  {icon} {row['regime']:<8} {int(row['n_trades']):>5} "
                f"{row['win_rate_pct']:>6.1f}% "
                f"{exp_r:>+8.3f}R "
                f"{row['avg_win_r']:>+6.2f}R "
                f"{row['avg_loss_r']:>+6.2f}R "
                f"{row['total_r']:>+8.1f}R{flag}"
            )
    else:
        print("  Not enough regime diversity in resolved trades.")

    # ── Score Correlation ────────────────────────────────────────────────────
    _section("SIGNAL QUALITY — DOES SCORE PREDICT OUTCOMES?")
    corr = compute_score_correlation(resolved)

    if corr:
        sc = corr.get("score_vs_r_corr", None)
        if sc is not None:
            predictive = "✓ YES" if corr.get("score_predictive") else "✗ WEAK"
            print(f"  Score vs R-multiple correlation : {sc:+.3f}  →  {predictive}")
            if sc < 0:
                print("  ⚠  Negative correlation: higher scores are performing WORSE.")
                print("     The scoring weights need recalibration.")

        if corr.get("ranked_by_predictiveness"):
            print(f"\n  Metric predictiveness (correlation with R-multiple):")
            print(f"  {'METRIC':<25} {'CORRELATION':>12}  NOTE")
            print("  " + "─" * 55)
            for metric, c in corr["ranked_by_predictiveness"]:
                direction = "↑ pos" if c > 0 else "↓ neg"
                strength  = ("STRONG" if abs(c) > 0.30 else
                             "MODERATE" if abs(c) > 0.15 else "WEAK")
                print(f"  {metric:<25} {c:>+12.3f}  {strength} {direction}")

    print("\n" + "═" * 70 + "\n")


# ═══════════════════════════════════════════════════
# REPORT 3 — EQUITY CURVE
# ═══════════════════════════════════════════════════
def print_equity_report(journal_dir: str = "journal") -> None:
    """
    Simulates account growth on the resolved trade sequence.
    Shows compounding effect, max drawdown, time underwater.
    """
    df       = load_trade_log(journal_dir)
    resolved = df[df["r_multiple"].notna()].copy()

    capital   = CONFIG["account_capital"]
    risk_frac = CONFIG["max_risk_per_trade"]

    print("\n" + "═" * 70)
    print("  EQUITY CURVE SIMULATION")
    print(f"  Initial capital: ₹{capital:,.0f}  |  Risk/trade: {risk_frac*100:.1f}%")
    print("═" * 70)

    if len(resolved) < MIN_ANALYSIS_SAMPLE:
        print(f"  Need {MIN_ANALYSIS_SAMPLE}+ resolved trades. Currently: {len(resolved)}")
        print("═" * 70 + "\n")
        return

    sim = simulate_equity_curve(resolved, capital, risk_frac)

    if not sim:
        print("  Could not run simulation.")
        print("═" * 70 + "\n")
        return

    _section("SIMULATION RESULTS")
    ret_icon = "🟢" if sim["total_return_pct"] > 0 else "🔴"
    dd_icon  = "🟢" if sim["max_drawdown_pct"] < 10 else ("🟡" if sim["max_drawdown_pct"] < 20 else "🔴")

    print(f"  Trades simulated     : {sim['n_trades']}")
    print(f"  Starting capital     : ₹{sim['initial_capital']:>12,.2f}")
    print(f"  Final capital        : ₹{sim['final_capital']:>12,.2f}")
    print(f"  Total return         : {ret_icon} {sim['total_return_pct']:+.2f}%")
    print(f"  Max drawdown         : {dd_icon} {sim['max_drawdown_pct']:.2f}%")
    print(f"  % time underwater    : {sim['underwater_pct']:.1f}%  (not at equity highs)")

    # ASCII equity sparkline (20-char wide)
    _section("EQUITY SPARKLINE  (simulated)")
    _print_sparkline(sim["equity_series"])

    # Drawdown interpretation
    _section("RISK INTERPRETATION")
    max_dd    = sim["max_drawdown_pct"]
    risk_unit = capital * risk_frac

    print(f"  At 1% risk/trade, worst-case streak :")
    for streak in [3, 5, 7, 10]:
        dd_est = (1 - (0.99 ** streak)) * 100
        inr    = capital * (1 - 0.99**streak)
        print(f"    {streak} consecutive losses → {dd_est:.1f}% drawdown (₹{inr:,.0f})")

    print(f"\n  Actual max drawdown experienced    : {max_dd:.2f}%")
    print(f"  Recommended max drawdown ceiling   : 15.0%  (consider reducing size above this)")

    print("\n" + "═" * 70 + "\n")


def _print_sparkline(equity: list, width: int = 60, height: int = 8) -> None:
    """
    Renders a simple ASCII equity curve in the terminal.
    Not beautiful — but you can see the shape immediately.
    """
    if len(equity) < 2:
        print("  (not enough data for sparkline)")
        return

    # Downsample to width points
    step   = max(1, len(equity) // width)
    sample = equity[::step]

    mn     = min(sample)
    mx     = max(sample)
    rng    = mx - mn if mx != mn else 1

    bars   = "▁▂▃▄▅▆▇█"
    line   = ""
    for val in sample:
        idx  = int((val - mn) / rng * (len(bars) - 1))
        line += bars[idx]

    # Split into rows
    print(f"  ₹{mx:>12,.0f} ┐")
    print(f"             │ {line}")
    print(f"  ₹{mn:>12,.0f} ┘")
    print(f"             0{'─' * (len(line) - 2)}{len(equity)-1} trades")


# ═══════════════════════════════════════════════════
# REPORT 4 — SCAN LOG SUMMARY
# ═══════════════════════════════════════════════════
def print_scan_log_summary(journal_dir: str = "journal") -> None:
    """
    Shows how much data has been accumulated in the scan log.
    Useful for understanding the universe of opportunities vs taken trades.
    """
    summary = get_scan_summary(journal_dir)
    scan_df = load_scan_log(journal_dir)

    print("\n" + "═" * 70)
    print("  SCAN LOG SUMMARY  (all scanner output, not just taken trades)")
    print("═" * 70)

    if summary["total_rows"] == 0:
        print("  No scan data yet. Run the scanner to start accumulating data.")
        print("═" * 70 + "\n")
        return

    print(f"  Total scan records   : {summary['total_rows']}")
    print(f"  Scanning days        : {summary['unique_dates']}")
    print(f"  Unique tickers seen  : {summary['unique_tickers']}")
    print(f"  Date range           : {summary['date_range']}")

    _section("GRADE DISTRIBUTION  (across all scans)")
    for grade, count in sorted(summary["grade_counts"].items(),
                                key=lambda x: x[0]):
        icon = _grade_colour(grade)
        pct  = count / summary["total_rows"] * 100
        bar  = "█" * int(pct / 2)
        print(f"  {icon}{grade:<6} {count:>5}  {pct:>5.1f}%  {bar}")

    _section("STATUS DISTRIBUTION  (across all scans)")
    for status, count in sorted(summary["status_counts"].items(),
                                  key=lambda x: -x[1]):
        pct  = count / summary["total_rows"] * 100
        bar  = "█" * int(pct / 2)
        print(f"  {status:<10} {count:>5}  {pct:>5.1f}%  {bar}")

    # Conversion rate: how many scan candidates did you actually trade?
    trade_df = load_trade_log(journal_dir)
    if not trade_df.empty:
        _section("SCAN → TRADE CONVERSION")
        n_scan_candidates = len(scan_df[scan_df["grade"].isin(["A+", "A", "B"])])
        n_taken           = len(trade_df)
        conv_rate         = n_taken / n_scan_candidates * 100 if n_scan_candidates > 0 else 0
        print(f"  Graded candidates (A+/A/B) : {n_scan_candidates}")
        print(f"  Trades actually taken      : {n_taken}")
        print(f"  Conversion rate            : {conv_rate:.1f}%")
        if conv_rate > 40:
            print("  ⚠  High conversion rate — are you being selective enough?")
        elif conv_rate < 5 and n_scan_candidates > 20:
            print("  ⚠  Very low conversion — are you following your own signals?")

    print("\n" + "═" * 70 + "\n")


# ═══════════════════════════════════════════════════
# MASTER REPORT
# ═══════════════════════════════════════════════════
def print_full_report(journal_dir: str = "journal") -> None:
    """
    Runs all reports in sequence.
    The weekly review ritual — takes ~10 seconds, replaces hours of guesswork.
    """
    print_expectancy_report(journal_dir)
    print_segment_report(journal_dir)
    print_edge_intelligence_report(journal_dir)
    print_equity_report(journal_dir)
    print_scan_log_summary(journal_dir)


# ═══════════════════════════════════════════════════
# REPORT 5 — EDGE INTELLIGENCE
# ═══════════════════════════════════════════════════
# Bucketed expectancy + MFE/MAE distributions + rolling stability +
# rolling drawdown + duration analysis. The "where does my edge live?" report.

def _print_bucket_table(seg_df, bucket_col: str, header_label: str,
                        flag_threshold: float = -0.10, min_n_for_flag: int = 5) -> None:
    """Renders a bucketed expectancy table identically across all dimensions."""
    if seg_df is None or seg_df.empty:
        print(f"  No data for {header_label.lower()}.")
        return

    hdr = (f"  {header_label:<18} {'N':>5} {'WIN%':>7} {'EXP(R)':>9} "
           f"{'AVG_W':>7} {'AVG_L':>7} {'TOTAL_R':>9}")
    print(hdr)
    print("  " + "─" * 64)
    for _, row in seg_df.iterrows():
        exp_r = row["expectancy_r"]
        flag  = " ← WEAK" if exp_r < flag_threshold and row["n_trades"] >= min_n_for_flag else ""
        if   exp_r >=  0.30: marker = "🟢"
        elif exp_r >=  0.00: marker = "🟡"
        else:                marker = "🔴"
        print(
            f"  {marker} {str(row[bucket_col]):<15} "
            f"{int(row['n_trades']):>5} "
            f"{row['win_rate_pct']:>6.1f}% "
            f"{exp_r:>+8.3f}R "
            f"{row['avg_win_r']:>+6.2f}R "
            f"{row['avg_loss_r']:>+6.2f}R "
            f"{row['total_r']:>+8.1f}R{flag}"
        )


def _print_histogram(histogram: list, max_bar: int = 30) -> None:
    """Renders an ASCII histogram from [(label, count), ...] pairs."""
    if not histogram:
        return
    counts = [c for _, c in histogram]
    peak   = max(counts) if counts else 1
    if peak == 0:
        peak = 1
    for label, count in histogram:
        bar = "█" * int(count / peak * max_bar)
        print(f"    {label:<22} {count:>4}  {bar}")


def _print_dd_sparkline(dd_series: list, width: int = 60) -> None:
    """Compact ASCII drawdown curve. dd values are <= 0."""
    if not dd_series or len(dd_series) < 2:
        print("    (not enough data)")
        return
    step    = max(1, len(dd_series) // width)
    sample  = dd_series[::step]
    mn      = min(sample)              # most negative
    rng     = abs(mn) if mn != 0 else 1
    bars    = "▁▂▃▄▅▆▇█"
    # deeper drawdown = taller bar (we flip the sign)
    line = "".join(bars[int(abs(v) / rng * (len(bars) - 1))] for v in sample)
    print(f"    {0:>6.1f}%  ┐")
    print(f"             │ {line}")
    print(f"    {mn:>6.1f}%  ┘  (deepest)")


def print_edge_intelligence_report(journal_dir: str = "journal") -> None:
    """
    Statistical intelligence: WHERE does the edge actually live?
    Bucketed expectancy + distributions + rolling stability + duration.
    """
    try:
        from config.config import CONFIG as _CFG
    except ModuleNotFoundError:
        _CFG = CONFIG

    df       = load_trade_log(journal_dir)
    resolved = df[df["r_multiple"].notna()].copy()

    print("\n" + "═" * 70)
    print("  EDGE INTELLIGENCE  —  where the system actually has edge")
    print("═" * 70)

    if len(resolved) < MIN_ANALYSIS_SAMPLE:
        print(f"  Need {MIN_ANALYSIS_SAMPLE}+ resolved trades. Currently: {len(resolved)}")
        print("═" * 70 + "\n")
        return

    if len(resolved) < MIN_RELIABLE_SAMPLE:
        print(f"  ⚠  Low sample ({len(resolved)} trades) — treat splits as directional, not decisive.\n")

    # ── 1. EXPECTANCY BY SECTOR ────────────────────────────────────────────
    _section("EXPECTANCY BY SECTOR")
    sec_df = compute_sector_analysis(resolved, _CFG)
    if not sec_df.empty:
        sec_df = sec_df.sort_values("expectancy_r", ascending=False)
    _print_bucket_table(sec_df, "sector", "SECTOR")

    # ── 2. EXPECTANCY BY ATR BUCKET ────────────────────────────────────────
    _section("EXPECTANCY BY ATR VOLATILITY BUCKET")
    print(f"  {_d('Are calmer or wilder names paying you?')}")
    atr_df = compute_atr_bucket_analysis(resolved)
    _print_bucket_table(atr_df, "atr_bucket", "ATR BUCKET")

    # ── 3. EXPECTANCY BY RS BUCKET ─────────────────────────────────────────
    _section("EXPECTANCY BY RELATIVE-STRENGTH BUCKET")
    print(f"  {_d('Does leadership strength translate into R?')}")
    rs_df = compute_rs_bucket_analysis(resolved)
    _print_bucket_table(rs_df, "rs_bucket", "RS BUCKET")

    # ── 4. MFE / MAE DISTRIBUTIONS ─────────────────────────────────────────
    _section("MFE / MAE DISTRIBUTIONS  (excursion quality)")
    dist = compute_mfe_mae_distribution(resolved)
    if dist:
        mfe_p = dist["mfe_percentiles"]
        mae_p = dist["mae_percentiles"]
        print(f"  Sample: {dist['n']} resolved trades\n")
        print(f"  MFE percentiles (max favourable excursion, %):")
        print(f"    p10={mfe_p['p10']:>6.2f}  p25={mfe_p['p25']:>6.2f}  "
              f"med={mfe_p['median']:>6.2f}  p75={mfe_p['p75']:>6.2f}  "
              f"p90={mfe_p['p90']:>6.2f}  max={mfe_p['max']:>6.2f}")
        print(f"\n  MAE percentiles (max adverse excursion, %):")
        print(f"    p10={mae_p['p10']:>6.2f}  p25={mae_p['p25']:>6.2f}  "
              f"med={mae_p['median']:>6.2f}  p75={mae_p['p75']:>6.2f}  "
              f"p90={mae_p['p90']:>6.2f}  min={mae_p['min']:>6.2f}")

        print(f"\n  MFE distribution:")
        _print_histogram(dist["mfe_histogram"])
        print(f"\n  MAE distribution:")
        _print_histogram(dist["mae_histogram"])
    else:
        print("  No MFE/MAE data available.")

    # ── 5. ROLLING EXPECTANCY ──────────────────────────────────────────────
    _section("ROLLING EXPECTANCY  (is the edge stable?)")
    roll = compute_rolling_expectancy(resolved, window=20)
    if roll:
        trend_icon = {"IMPROVING": "🟢", "STABLE": "🟡", "DECAYING": "🔴"}.get(roll["trend"], "⚪")
        print(f"  Window               : last {roll['window']} trades")
        print(f"  Lifetime expectancy  : {roll['lifetime_exp_r']:+.3f}R")
        print(f"  Recent expectancy    : {roll['recent_exp_r']:+.3f}R")
        print(f"  Trend                : {trend_icon} {roll['trend']}")
        if roll["trend"] == "DECAYING":
            print(f"\n  ⚠  Recent {roll['window']}-trade window is materially worse than lifetime.")
            print(f"     Pause new entries → review the last {roll['window']} losers before resuming.")
        elif roll["trend"] == "IMPROVING":
            print(f"\n  ✓  Recent window outperforming lifetime — system is in form.")
    else:
        print(f"  Need 20+ resolved trades for rolling window. Currently: {len(resolved)}")

    # ── 6. ROLLING DRAWDOWN ────────────────────────────────────────────────
    _section("DRAWDOWN PROFILE  (simulated equity)")
    sim = simulate_equity_curve(
        resolved,
        _CFG["account_capital"],
        _CFG["max_risk_per_trade"],
    )
    if sim:
        dd = compute_rolling_drawdown(sim["equity_series"])
        if dd:
            dd_icon = "🟢" if abs(dd["max_dd_pct"]) < 10 else ("🟡" if abs(dd["max_dd_pct"]) < 20 else "🔴")
            print(f"  Peak drawdown        : {dd_icon} {dd['max_dd_pct']:.2f}%")
            print(f"  Current drawdown     : {dd['current_dd_pct']:.2f}%")
            print(f"  Longest underwater   : {dd['longest_underwater']} trades")
            print(f"  Time underwater      : {dd['underwater_pct']:.1f}% of journey")
            print()
            _print_dd_sparkline(dd["dd_series"])
    else:
        print("  Equity simulation unavailable.")

    # ── 7. TRADE DURATION ──────────────────────────────────────────────────
    _section("TRADE DURATION ANALYSIS")
    dur = compute_duration_analysis(resolved)
    if dur:
        print(f"  {'COHORT':<10} {'N':>5} {'MEAN':>7} {'MEDIAN':>8} {'P90':>7} {'MAX':>6}")
        print("  " + "─" * 50)
        for label, key in [("All", "all"), ("Winners", "winners"), ("Losers", "losers")]:
            s = dur[key]
            if s.get("n", 0) == 0:
                continue
            print(f"  {label:<10} {s['n']:>5} {s['mean']:>6.1f}d {s['median']:>7.1f}d "
                  f"{s['p90']:>6.1f}d {s['max']:>5}d")

        print(f"\n  Duration distribution:")
        _print_histogram(dur["buckets"])

        # Interpretation
        w, l = dur["winners"], dur["losers"]
        if w.get("n", 0) >= 5 and l.get("n", 0) >= 5:
            if w["mean"] > l["mean"] * 1.5:
                print(f"\n  ✓  Healthy profile: winners ride ~{w['mean']/max(l['mean'],0.1):.1f}× longer than losers.")
            elif l["mean"] > w["mean"] * 1.2:
                print(f"\n  ⚠  Losers linger ({l['mean']:.1f}d) longer than winners ({w['mean']:.1f}d).")
                print(f"     Consider tighter time-stops or earlier failed-breakout exits.")
    else:
        print(f"  Need {MIN_ANALYSIS_SAMPLE}+ resolved trades with days_held data.")

    print("\n" + "═" * 70 + "\n")


def _d(text: str) -> str:
    """Local dim helper — keeps reports.py free of cross-module imports."""
    return f"\033[2m{text}\033[0m"
