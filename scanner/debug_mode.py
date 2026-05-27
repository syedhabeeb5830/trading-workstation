"""
scanner/debug_mode.py  —  Full Pipeline Observability (Phase 6)
================================================================
python run.py --debug

Shows all tickers, all scores, all rejection reasons.
Groups: PASSED → NEAR MISSES → HARD REJECTS
Shows 3-dimensional score breakdown for every scored ticker.
NOT for daily trading — for strategy refinement only.
"""

import os
import sys
from datetime import datetime

_G, _Y, _R, _C, _B, _D, _RST = (
    "\033[92m", "\033[93m", "\033[91m",
    "\033[96m", "\033[1m", "\033[2m", "\033[0m"
)
def _g(s): return f"{_G}{s}{_RST}"
def _y(s): return f"{_Y}{s}{_RST}"
def _r(s): return f"{_R}{s}{_RST}"
def _c(s): return f"{_C}{s}{_RST}"
def _b(s): return f"{_B}{s}{_RST}"
def _d(s): return f"{_D}{s}{_RST}"

W = 62
def _rule(): print("═" * W)
def _thin(): print("─" * W)
def _ln():   print()


def _score_breakdown_3d(r, config: dict) -> list[str]:
    """3-dimensional score breakdown for debug display."""
    lines = []

    def _bar(v): 
        bl = int(v / 100 * 20)
        return "█" * bl + "░" * (20 - bl)

    lines.append(f"    {'Dimension':<22}  {'Bar':<22}  Score")
    lines.append(f"    {'─'*55}")
    lines.append(f"    {'SETUP  (chart quality)':<22}  {_bar(r.setup_score)}"
                 f"  {_b(str(r.setup_score))}")
    lines.append(f"    {'EXEC   (tradability)':<22}  {_bar(r.exec_score)}"
                 f"  {r.exec_score}")
    lines.append(f"    {'REGIME (market env)':<22}  {_bar(r.regime_score)}"
                 f"  {r.regime_score}")
    lines.append(f"    {'─'*55}")
    cs = r.composite_score
    lines.append(f"    {'COMPOSITE (60/25/15)':<22}  {_bar(cs)}  {_b(str(cs))}")

    if r.metrics:
        m   = r.metrics
        w   = config["setup_weights"]
        _ln_str = ""
        lines.append("")
        lines.append(f"    {'Setup sub-components:'}")

        def _sub(name, val, weight):
            return (f"    {name:<26}  {_bar(int(val*100)):>20}  "
                    f"{val:.2f} × {weight:.2f} = {val*weight*100:.1f}pt")

        rs_raw   = min(1.0, max(0.0, (m["relative_strength"] + 10) / 25))
        t_raw    = m["trend"]["score_norm"]
        rng      = m["consolidation"]["range_pct"]
        c_raw    = min(1.0, max(0.0, 1 - rng / 6.0))
        vol      = m["volume"]["volume_ratio"]
        v_raw    = min(1.0, max(0.0, 1 - vol))

        lines.append(_sub("trend_quality",      t_raw,  w["trend_quality"]))
        lines.append(_sub("relative_strength",  rs_raw, w["relative_strength"]))
        lines.append(_sub("compression_quality",c_raw,  w["compression_quality"]))
        lines.append(_sub("volume_behavior",    v_raw,  w["volume_behavior"]))

    return lines


def _print_ticker_debug(r, config: dict) -> None:
    if r.passed:
        hclr, sym = _g, "✓"
    elif r.is_near_miss:
        hclr, sym = _y, "△"
    else:
        hclr, sym = _r, "✗"

    tier_str = ""
    if r.plan:
        tier_str = f"  [{r.plan.get('tier','?')}]  setup {r.setup_score:.0f}"

    _thin()
    print(f"  {hclr(sym + '  ' + r.ticker)}  {_d('stage: ' + r.stage)}{tier_str}")

    # Metrics
    parts = []
    if r.price:        parts.append(f"₹{r.price:,.0f}")
    if r.atr_pct:      parts.append(f"ATR {r.atr_pct:.1f}%")
    if r.trend_score:  parts.append(f"trend {r.trend_score}/4")
    if r.rs != 0:      parts.append(f"RS {r.rs:+.1f}%")
    if r.range_pct:    parts.append(f"rng {r.range_pct:.1f}%")
    if r.volume_ratio: parts.append(f"vol {r.volume_ratio:.2f}×")
    if r.distance_pct: parts.append(f"dist {r.distance_pct:.1f}%")
    if parts:
        print(f"     {_d('  ·  '.join(parts))}")

    for reason in r.rejection_reasons:
        print(f"     {_r('✗  ' + reason)}")
    for note in r.caution_notes:
        print(f"     {_y('△  ' + note)}")
    if r.is_near_miss:
        print(f"     {_y('Near-miss: ' + str(r.near_miss_score) + '/100')}")

    if r.setup_score > 0 or r.exec_score > 0:
        _ln()
        print(f"     {_b('3D Scores:')}")
        for line in _score_breakdown_3d(r, config):
            print("  " + line)

    if r.plan and r.passed:
        p = r.plan
        _ln()
        print(f"     {_b('Trade plan:')}")
        print(f"       Entry ₹{p['entry_price']:,.2f}  Stop ₹{p['stop_price']:,.2f}"
              f"  T1 ₹{p['t1']:,.2f}  RR {p['rr_t1']:.1f}x")
        print(f"       Tier: {p['tier']}  Status: {p['status']}"
              f"  Qty: {p['quantity']:,}  MaxLoss: ₹{p['max_loss_inr']:,.0f}")
        print(f"       Sector: {p.get('sector','?')}  "
              f"TierSize: {p.get('tier_size_pct',0)}%"
              f"  HeatLeft: {p.get('heat_remaining_pct',0):.1f}%")


def run_debug_mode(journal_dir: str = "journal") -> None:
    """python run.py --debug"""
    try:
        from config.config import CONFIG
        from scanner.scan_result import (
            run_observable_scan, get_near_misses, get_breadth_funnel
        )
        from scanner.portfolio import load_portfolio_state
    except ModuleNotFoundError:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from config.config import CONFIG
        from scanner.scan_result import (
            run_observable_scan, get_near_misses, get_breadth_funnel
        )
        from scanner.portfolio import load_portfolio_state

    ts = datetime.now().strftime("%d %b %Y  %H:%M")
    _rule()
    print(f"  {_b('SCANNER DEBUG MODE')}  —  full pipeline audit")
    print(f"  {_d(ts)}")
    print(f"  {_r('NOT FOR DAILY TRADING')}")
    _rule()
    _ln()

    print(f"  {_d('Running scan...')}  ", end="\r", flush=True)
    portfolio = load_portfolio_state(journal_dir)
    regime, results = run_observable_scan(
        CONFIG, open_risk_inr=portfolio["open_risk_inr"]
    )
    funnel = get_breadth_funnel(results)
    print(" " * 50, end="\r")

    r_str  = regime["regime"]
    r_icon = {"BULL": "🟢", "NEUTRAL": "🟡", "BEAR": "🔴"}.get(r_str, "⚪")
    sstr   = f"(strength {regime['strength']:.2f})"

    print(f"  {_b('MARKET')}  {r_icon}  {r_str}  {_d(sstr)}")
    _ln()

    # Funnel
    print(f"  {_b('PIPELINE FUNNEL')}")
    _thin()
    stages = [
        ("Scanned",           funnel["total"],          None),
        ("→ Had data",        funnel["passed_data"],    None),
        ("→ Liquid",          funnel["passed_liq"],     None),
        ("→ ATR ok",          funnel["passed_atr"],     None),
        ("→ Trending",        funnel["passed_trend"],   None),
        ("→ Coiling",         funnel["passed_filters"], None),
        ("→ Scored",          funnel["scored"],         None),
        ("→ ACTIONABLE",      funnel["actionable"],     _g),
    ]
    total = funnel["total"] or 1
    for label, n, clr in stages:
        bar_len = int(n / total * 24)
        bar     = "█" * bar_len
        val     = clr(str(n)) if clr else str(n)
        print(f"  {label:<26}  {val:>3}  {_d(bar)}")
    _thin()
    _ln()

    # Portfolio heat
    print(f"  {_b('PORTFOLIO STATE')}")
    _thin()
    print(f"  Open positions: {portfolio['position_count']}  |  "
          f"Heat: {portfolio['heat_used_pct']:.1f}%  |  "
          f"Remaining: {portfolio['heat_remaining_pct']:.1f}%")
    if portfolio["sector_counts"]:
        for sec, cnt in portfolio["sector_counts"].items():
            print(f"    {sec}: {cnt} position(s)")
    _thin()
    _ln()

    passed = [r for r in results if r.passed]
    print(f"  {_b('PASSED')}  ({len(passed)})")
    if passed:
        for r in sorted(passed, key=lambda x: -x.setup_score):
            _print_ticker_debug(r, CONFIG)
    else:
        print(f"  {_d('  None')}")
    _ln()

    near = [r for r in results if r.is_near_miss]
    near.sort(key=lambda r: r.near_miss_score, reverse=True)
    print(f"  {_b('NEAR MISSES')}  ({len(near)})")
    if near:
        for r in near:
            _print_ticker_debug(r, CONFIG)
    else:
        print(f"  {_d('  None')}")
    _ln()

    hard = [r for r in results if r.is_hard_reject]
    print(f"  {_b('HARD REJECTS')}  ({len(hard)})  {_d('structural — not near-misses')}")
    for r in hard:
        reason = r.rejection_reasons[0] if r.rejection_reasons else "?"
        print(f"  {_d('  ✗  ' + r.ticker + '  — ' + reason)}")

    _ln()
    _rule()
    print(f"\n  {_d('Debug complete. Actionable: ' + str(funnel['actionable']) + ' / ' + str(funnel['total']))}")
    print(f"  {_d('Run python run.py --today for execution cockpit.')}\n")