"""
scanner/daily_mode.py  —  Daily Execution Cockpit (Phase 6)
============================================================
Command: python run.py --today

Answers every morning:
  1. Market regime decision
  2. Portfolio heat status
  3. What to trade (tiers, not binary)
  4. Why nothing qualifies (near misses)
  5. Open position management reminders

KEY CHANGES:
  - Tier system replaces grade (TIER_1/TIER_2/PILOT/WATCH/AVOID)
  - Portfolio heat shown in header
  - Sector crowding warnings shown on cards
  - Regime context: explains what tier is available today
  - Near misses always shown — never silent "no action"
"""

import os
import sys
from datetime import datetime, date

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

W   = 72
LBL = 15

def _ln():   print()
def _rule(): print("═" * W)
def _thin(): print("─" * W)
def _row(label, value, vfn=None):
    v = vfn(value) if vfn else value
    print(f"  {label:<{LBL}}{v}")


def _live_suffix(p: dict) -> str:
    """Builds a coloured '+1.2%  LIVE' suffix for execution card CURRENT row."""
    src = p.get("price_src", "EOD")
    if src != "LIVE":
        return f"   {_d('EOD')}"
    chg = float(p.get("live_change_pct", 0.0) or 0.0)
    chg_clr = _g if chg > 0 else (_r if chg < 0 else _d)
    return f"   {chg_clr(f'{chg:+.2f}%')}  {_g('LIVE')}"


# ── Tier display helpers ───────────────────────────────────────────────────────

TIER_META = {
    "TIER_1": {"icon": "★★", "clr": _g, "size": "100% size", "label": "TIER 1 — ELITE"},
    "TIER_2": {"icon": "★",  "clr": _g, "size": "75% size",  "label": "TIER 2 — STRONG"},
    "PILOT":  {"icon": "◑",  "clr": _y, "size": "35% size",  "label": "PILOT — REDUCED"},
    "WATCH":  {"icon": "○",  "clr": _d, "size": "0% — wait", "label": "WATCH"},
    "AVOID":  {"icon": "✗",  "clr": _r, "size": "skip",      "label": "AVOID"},
}

STATUS_CLR = {
    "READY":    _g,
    "WATCH":    _y,
    "EXTENDED": _y,
    "GAPPED":   _r,
    "AVOID":    _r,
}


# ── Market header ──────────────────────────────────────────────────────────────

_REGIME_META = {
    "BULL":    {"icon": "🟢", "clr": _g,
                "decision": "NORMAL RISK — all tiers available",
                "advice":   "Trade TIER_1 and TIER_2 at full/75% size."},
    "NEUTRAL": {"icon": "🟡", "clr": _y,
                "decision": "SELECTIVE — capped at TIER_2 (75% size max)",
                "advice":   "No TIER_1 today. TIER_2 and PILOT only."},
    "BEAR":    {"icon": "🔴", "clr": _r,
                "decision": "CAPITAL PRESERVATION — no new longs",
                "advice":   "Manage existing positions only."},
}

def _print_header(regime: dict, funnel: dict, portfolio: dict) -> None:
    meta = _REGIME_META.get(regime["regime"], _REGIME_META["NEUTRAL"])
    clr  = meta["clr"]
    ts   = datetime.now().strftime("%d %b %Y  %H:%M")
    sstr = f"(strength {regime['strength']:.2f})"

    _rule()
    print(f"  {_b('SWING TRADER — DAILY COCKPIT')}")
    print(f"  {_d(ts)}")
    _rule()
    _ln()

    print(f"  {_b('MARKET')}  {meta['icon']}  {clr(_b(regime['regime']))}  {_d(sstr)}")
    print(f"  {clr(meta['decision'])}")
    print(f"  {_d(meta['advice'])}")
    _ln()

    # Capital line — live from Kite when available, else config fallback
    try:
        from scanner.capital import get_effective_capital
        from config.config   import CONFIG as _CFG
        cap = get_effective_capital(_CFG)
        src_badge = _g("LIVE") if cap.source == "kite" else _d("CONFIG")
        cap_line = (
            f"  {_b('CAPITAL')}  ₹{cap.capital:>10,.0f}   "
            f"{_b('AVAIL')} ₹{cap.available:>10,.0f}   "
            f"{_b('DEPLOYED')} {cap.deployed_pct:>4.1f}%   "
            f"{src_badge}"
        )
        print(cap_line)
    except Exception:
        pass

    # Portfolio heat bar
    heat      = portfolio["heat_used_pct"]
    heat_max  = 5.0   # percent
    heat_bar  = int(heat / heat_max * 20)
    heat_clr  = _g if heat < 3.0 else (_y if heat < 4.5 else _r)
    bar       = heat_clr("█" * heat_bar) + _d("░" * (20 - heat_bar))
    pos_count = portfolio["position_count"]
    print(f"  {_b('HEAT')}     {bar}  {heat_clr(f'{heat:.1f}%')}  "
          f"{_d(f'({pos_count}/6 open positions)')}")

    # Breadth funnel
    f = funnel
    funnel_str = (
        f"{f['total']} scanned  →  {f['passed_liq']} liquid"
        f"  →  {f['passed_trend']} trending"
        f"  →  {f['passed_filters']} coiling"
        f"  →  {_b(str(f['actionable'])) if f['actionable'] else _d('0')} actionable"
    )
    print(f"  {_d(funnel_str)}")
    _thin()


# ── Open positions ─────────────────────────────────────────────────────────────

def _get_open_positions(journal_dir: str) -> list[dict]:
    """Delegates to scanner.positions — single source of truth."""
    try:
        from scanner.positions import load_active_positions
        from config.config     import CONFIG
    except ModuleNotFoundError:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from scanner.positions import load_active_positions
        from config.config     import CONFIG
    return load_active_positions(journal_dir, CONFIG)


def _print_open_positions(positions: list[dict]) -> None:
    if not positions: return
    _ln()
    note = "— run --positions for full management view"
    print(f"  {_b('OPEN POSITIONS')}  ({len(positions)} active)  {_d(note)}")
    _thin()
    for p in positions:
        r        = p["r_current"]
        clr      = _g if r >= 1.0 else (_y if r >= 0 else _r)
        dist     = p.get("dist_to_t1", 0)
        days     = p.get("days_held", 0)

        t1_str   = f"   {_d(f'→T1: {dist:+.1f}%')}" if dist else ""
        days_str = _d(f"{days}d held")

        print(f"  {_c(p['ticker'])}  {clr(f'{r:+.1f}R')}   {days_str}{t1_str}")
        entry_line = (
            f"Entry ₹{p['entry']:,.0f}  Now ₹{p['current']:,.0f}  "
            f"Stop ₹{p['stop']:,.0f}"
        )
        print(f"     {_d(entry_line)}")

        priority = p.get("action_priority", 5)
        aclr     = _r if priority <= 1 else (_y if priority <= 3 else _d)
        print(f"     {aclr('→  ' + p['action'])}")
    _thin()


# ── Overview table ─────────────────────────────────────────────────────────────
# Column widths (plain-text, before ANSI wrapping)
_CW = {"num": 3, "ticker": 15, "tier": 13, "score": 5,
       "price": 8, "rr": 4, "qty": 6}

def _print_overview_table(actionable: list) -> None:
    if not actionable: return
    _ln()
    # Header — plain text, no ANSI, widths match data rows exactly
    print(
        f"  {'#':{_CW['num']}} "
        f"{'TICKER':{_CW['ticker']}} "
        f"{'TIER':{_CW['tier']}} "
        f"{'SCORE':>{_CW['score']}}  "
        f"{'ENTRY':>{_CW['price']}}  "
        f"{'STOP':>{_CW['price']}}  "
        f"{'T1':>{_CW['price']}}  "
        f"{'RR':>{_CW['rr']}}  "
        f"{'QTY':>{_CW['qty']}}  STATUS"
    )
    _thin()

    for i, p in enumerate(actionable, 1):
        tier  = p["tier"]
        tm    = TIER_META.get(tier, TIER_META["AVOID"])
        tclr  = tm["clr"]
        sclr  = STATUS_CLR.get(p["status"], _d)
        pok   = "" if p.get("portfolio_ok", True) else _r(" ⚠")

        # Pad plain text FIRST, then wrap in color so ANSI codes don't break alignment
        num_col    = str(i).ljust(_CW["num"])
        ticker_col = p["ticker"].ljust(_CW["ticker"])
        tier_raw   = f"{tm['icon']} {tier}"
        tier_col   = tier_raw.ljust(_CW["tier"])
        score_col  = f"{p['setup_score']:>{_CW['score']}.0f}"
        _ep = f"\u20b9{p['entry_price']:,.0f}"
        _sp = f"\u20b9{p['stop_price']:,.0f}"
        _tp = f"\u20b9{p['t1']:,.0f}"
        entry_col  = _ep.rjust(_CW["price"])
        stop_col   = _sp.rjust(_CW["price"])
        t1_col     = _tp.rjust(_CW["price"])
        rr_col     = f"{p['rr_t1']:.1f}x".rjust(_CW["rr"])
        qty_col    = f"{p['quantity']:,}".rjust(_CW["qty"])
        status_col = p["status"]

        print(
            f"  {num_col} "
            f"{_c(ticker_col)} "
            f"{tclr(tier_col)} "
            f"{score_col}  "
            f"{entry_col}  "
            f"{_r(stop_col)}  "
            f"{_g(t1_col)}  "
            f"{rr_col}  "
            f"{qty_col}  "
            f"{sclr(status_col)}{pok}"
        )

    _thin()
    for tier_key in ["TIER_1", "TIER_2", "PILOT"]:
        count = sum(1 for p in actionable if p["tier"] == tier_key)
        if count:
            tm = TIER_META[tier_key]
            print(f"  {tm['clr'](tm['icon'] + ' ' + str(count) + ' ' + tier_key)}", end="   ")
    ready = sum(1 for p in actionable if p["status"] == "READY")
    watch = sum(1 for p in actionable if p["status"] == "WATCH")
    if ready:
        print(f"  {_g(str(ready) + ' READY')}", end="")
    if watch:
        print(f"  {_y(str(watch) + ' WATCH')}", end="")
    print()


# ── Sector breakdown ───────────────────────────────────────────────────────────────────────────────────
def _print_sector_breakdown(actionable: list, open_counts: dict) -> None:
    """
    Shows planned exposure by sector after the overview table.
    Flags sectors with >=3 setups (open + READY) as concentration risk.
    Helps avoid taking 3 metal stocks thinking they're independent bets.
    """
    ready_by_sec = {}
    for p in actionable:
        if p.get("status") == "READY":
            sec = p.get("sector", "OTHER")
            ready_by_sec[sec] = ready_by_sec.get(sec, 0) + 1
    if not ready_by_sec and not open_counts:
        return
    sectors = sorted(set(list(ready_by_sec.keys()) + list(open_counts.keys())))
    _ln()
    print(f"  {_b('SECTOR EXPOSURE')}   {_d('(open + READY today)')}")
    for sec in sectors:
        op = open_counts.get(sec, 0)
        rd = ready_by_sec.get(sec, 0)
        total = op + rd
        if total == 0:
            continue
        clr = _r if total >= 3 else (_y if total >= 2 else _d)
        warn = "  ⚠  concentration" if total >= 3 else ""
        parts = []
        if op: parts.append(f"{op} open")
        if rd: parts.append(f"{rd} ready")
        print(f"    {clr(sec.ljust(12))}  {clr(str(total))}   {_d('(' + ', '.join(parts) + ')')}{_r(warn)}")


# ── Why bullets ────────────────────────────────────────────────────────────────

def _why_bullets(p: dict, regime: str) -> list[str]:
    bullets = []
    rs  = p.get("relative_strength", 0)
    rng = p.get("range_pct", 99)
    vol = p.get("volume_ratio", 1.0)
    ext = p.get("extension_pct", 0)
    ts  = p.get("trend_score", 0)
    tier = p.get("tier", "")

    bullets.append(
        _g(f"  ✓  Strong RS vs NIFTY  ({rs:+.1f}% / 20d)") if rs >= 5 else
        _g(f"  ✓  Positive RS vs NIFTY  ({rs:+.1f}%)")      if rs >= 2 else
        _y(f"  △  Marginal RS  ({rs:+.1f}%)")
    )
    bullets.append(
        _g(f"  ✓  Very tight coil  ({rng:.1f}% range, 5d)")  if rng <= 2.0 else
        _g(f"  ✓  Tight consolidation  ({rng:.1f}% range)")  if rng <= 4.0 else
        _y(f"  △  Loose consolidation  ({rng:.1f}%)")
    )
    bullets.append(
        _g(f"  ✓  Volume dried up sharply  ({vol:.2f}× avg)") if vol < 0.60 else
        _g(f"  ✓  Volume contracting  ({vol:.2f}× avg)")       if vol < 0.78 else
        _y(f"  △  Volume normal  ({vol:.2f}× avg)")
    )

    if ext < 0:
        d = abs(ext)
        bullets.append(
            _g(f"  ✓  Just below trigger  ({d:.1f}% to entry)") if d <= 1.5 else
            _g(f"  ✓  Near breakout  ({d:.1f}% to entry)")       if d <= 3.0 else
            _y(f"  △  {d:.1f}% away from trigger")
        )
    elif ext <= 2.0:
        bullets.append(_g(f"  ✓  At breakout  (+{ext:.1f}% above level)"))
    else:
        bullets.append(_y(f"  △  Extended  (+{ext:.1f}% above level)"))

    bullets.append(
        _g("  ✓  Clean trend  (price > 20 > 50 > 200 SMA)") if ts == 4 else
        _g("  ✓  Strong trend  (3/4 SMA conditions)")        if ts == 3 else
        _y("  △  Developing trend  (2/4 SMA conditions)")
    )

    if regime == "NEUTRAL" and tier == "TIER_2":
        bullets.append(_y("  △  Neutral regime — capped at 75% size (TIER_2)"))
    if regime == "NEUTRAL" and tier == "PILOT":
        bullets.append(_y("  △  Neutral regime — reduced to 35% size (PILOT)"))

    return bullets


# ── Execution card ──────────────────────────────────────────────────────────────

def _print_card(rank: int, p: dict, regime: str) -> None:
    tier   = p["tier"]
    status = p["status"]
    tm     = TIER_META.get(tier, TIER_META["AVOID"])
    sclr   = STATUS_CLR.get(status, _d)

    _rule()
    print(f"  {_d(str(rank) + '.'):<4}{_c(_b(p['ticker']))}   "
          f"{tm['clr'](tm['icon'] + ' ' + tier)}   {sclr(status)}")

    # Score row
    print(f"  {_d('Setup ' + str(p['setup_score']))}  "
          f"{_d('Exec ' + str(p['exec_score']))}  "
          f"{_d('Regime ' + str(p['regime_score']))}  "
          f"{_d('Size: ' + tm['size'])}")
    _thin()

    # Sector + portfolio note + staleness
    sector = p.get("sector", "OTHER")
    days_radar = int(p.get("days_on_radar", 0) or 0)
    sector_open = int(p.get("sector_open_count", 0) or 0)

    sector_str = f"Sector: {sector}"
    if sector_open >= 1:
        sector_str += f"  ({sector_open} already open)"
    print(f"  {_d(sector_str)}", end="")
    if p.get("sector_crowded"):
        print(f"  {_r('⚠ SECTOR CROWDED — skip if >=2 open')}", end="")
    if days_radar >= 3 and p.get("status") == "WATCH":
        print(f"  {_y(f'⚠ STALE PLAN — {days_radar}d on radar, re-check levels')}", end="")
    elif days_radar >= 1:
        print(f"  {_d(f'·  {days_radar}d on radar')}", end="")
    if not p.get("portfolio_ok", True):
        print(f"  {_r('⚠ ' + p.get('portfolio_note', ''))}", end="")
    print()
    _ln()

    _row("CURRENT",   f"₹{p['current_close']:>10,.2f}" + _live_suffix(p))
    _row("ENTRY",     f"₹{p['entry_price']:>10,.2f}",  _b)
    _row("STOP",      f"₹{p['stop_price']:>10,.2f}",   _r)
    _row("TARGET 1",  f"₹{p['t1']:>10,.2f}",           _g)
    _row("TARGET 2",  f"₹{p['t2']:>10,.2f}",           _g)
    _ln()
    _row("QTY",       f"{p['quantity']:,} shares  {_d('(' + tm['size'] + ')')}")
    _row("MAX LOSS",  f"₹{p['max_loss_inr']:>10,.0f}",  _r)
    cap_note = f"({p['capital_pct']:.1f}% of account)"
    _row("CAPITAL",   f"₹{p['capital_deployed']:>10,.0f}  {_d(cap_note)}")
    _row("RR",        f"{p['rr_t1']:.1f}x  →  T2 {p['rr_t2']:.1f}x", _g)
    rps_note = f"(₹{p['risk_per_share']:.0f}/share)"
    _row("STOP TYPE", f"{p['stop_type']}  {_d(rps_note)}")
    _ln()

    print(f"  {_b('WHY VALID')}")
    for line in _why_bullets(p, regime):
        print(line)

    # Cautions
    for note in p.get("caution_notes", []):
        print(f"  {_y('  △  ' + note)}")

    _ln()
    print(f"  {_b('ACTION')}")
    if status == "READY":
        print(_g(f"  →  Set buy-stop at ₹{p['entry_price']:,.2f} tonight."))
        print(_g( f"     {tm['size']} — ₹{p['max_loss_inr']:,.0f} max loss."))
    elif status == "WATCH":
        to_go = p["entry_price"] - p["current_close"]
        print(_y(f"  →  Alert at ₹{p['entry_price']:,.2f}  (₹{to_go:,.0f} away)."))
    elif status == "EXTENDED":
        print(_y( "  →  Do NOT chase. Wait for re-base."))
    elif status == "GAPPED":
        pct = p.get("gap_up_pct", 0)
        print(_r(f"  →  Gapped up {pct:.1f}% at open. Avoid chasing today."))
        print(_r( "     Place alert at original entry for next session."))

    _ln()


# ── Near misses ────────────────────────────────────────────────────────────────

def _print_near_misses(near_misses: list, regime: str) -> None:
    if not near_misses: return
    _ln()
    print(f"  {_b('NEAR MISSES')}  {_d('— closest to qualifying')}")
    _thin()
    for r in near_misses:
        nm   = r.near_miss_score
        bar  = _y("█" * int(nm/100*20)) + _d("░" * (20 - int(nm/100*20)))
        print(f"  {_c(r.ticker):<16}  {bar}  {_y(str(nm) + '/100')}")

        parts = []
        if r.rs != 0:       parts.append(f"RS {r.rs:+.1f}%")
        if r.range_pct:    parts.append(f"range {r.range_pct:.1f}%")
        if r.volume_ratio: parts.append(f"vol {r.volume_ratio:.2f}×")
        if r.distance_pct: parts.append(f"dist {r.distance_pct:.1f}%")
        if r.trend_score:  parts.append(f"trend {r.trend_score}/4")
        if r.atr_pct:      parts.append(f"ATR {r.atr_pct:.1f}%")
        if parts:
            print(f"  {_d('  ' + '  ·  '.join(parts))}")

        for reason in r.rejection_reasons[:2]:
            print(f"  {_r('  ✗  ' + reason)}")

        if r.setup_score > 0:
            ss = f"  setup {r.setup_score:.0f}"
            print(f"  {_d(ss)}")
        _ln()
    _thin()


# ── No-trade ────────────────────────────────────────────────────────────────────

def _print_no_trade(regime: dict, funnel: dict, portfolio: dict,
                    near_misses: list) -> None:
    meta       = _REGIME_META.get(regime["regime"], _REGIME_META["NEUTRAL"])
    icon       = meta["icon"]
    clr        = meta["clr"]
    sstr       = f"(strength {regime['strength']:.2f})"
    ts         = datetime.now().strftime("%d %b %Y  %H:%M")
    f          = funnel
    heat       = portfolio["heat_used_pct"]

    _rule()
    print(f"  {_b('SWING TRADER — DAILY COCKPIT')}")
    print(f"  {_d(ts)}")
    _rule()
    _ln()
    print(f"  {_b('MARKET')}  {icon}  {clr(_b(regime['regime']))}  {_d(sstr)}")
    print(f"  {clr(meta['decision'])}")
    _ln()

    funnel_str = (
        f"{f['total']} scanned  →  {f['passed_liq']} liquid"
        f"  →  {f['passed_trend']} trending"
        f"  →  {f['passed_filters']} coiling"
        f"  →  {_r('0')} actionable"
    )
    print(f"  {_d(funnel_str)}")
    _thin()
    _ln()

    if regime["regime"] == "BEAR":
        print(f"  {_r(_b('NO LONGS TODAY — BEAR REGIME'))}")
        _ln()
        print(f"  {_d('All long setups blocked. Preserve capital.')}")
        print(f"  {_d('Resume when NIFTY > 50 SMA.')}")
    else:
        print(f"  {_d(_b('NO ACTIONABLE SETUPS TODAY'))}")
        _ln()
        if f["passed_filters"] == 0 and f["passed_trend"] == 0:
            print(f"  {_d('Broad weakness — few stocks in uptrend.')}")
        elif f["passed_filters"] == 0:
            print(f"  {_d('Stocks trending but no tight coils found.')}")
        elif f["scored"] == 0:
            print(f"  {_d('Coils exist but RS too weak vs NIFTY.')}")
        else:
            print(f"  {_d('Setups scored but below deployment threshold.')}")
        print(f"  {_d('Quality over frequency. This is normal.')}")

    _print_near_misses(near_misses, regime["regime"])

    # Market pulse
    best = max((r.near_miss_score for r in near_misses), default=0)
    _ln()
    print(f"  {_b('MARKET PULSE')}")
    _thin()
    if best >= 75:
        print(f"  {_y('Close to setups — 1-2 conditions away. Watch carefully.')}")
    elif best >= 50:
        print(f"  {_d('Moderate proximity. Market building toward setups.')}")
    else:
        print(f"  {_d('Low proximity. Check again in 2-3 days.')}")

    if heat > 0:
        _ln()
        print(f"  {_d('Portfolio heat: ' + str(heat) + '% used. Open positions exist.')}")

    _ln()
    _rule()
    _ln()


# ── Footer ──────────────────────────────────────────────────────────────────────

def _print_footer(n_ready: int, n_watch: int, near_misses: list,
                  portfolio: dict, journal_dir: str) -> None:
    _ln()
    _rule()
    _ln()

    if n_ready > 0:
        print(_g(f"  {n_ready} order(s) READY — place buy-stops tonight."))
    if n_watch > 0:
        print(_y(f"  {n_watch} setup(s) on WATCH — set price alerts."))
    if n_ready == 0 and n_watch == 0:
        print(_d("  Nothing to act on. Review tomorrow."))

    heat = portfolio["heat_used_pct"]
    if heat > 0:
        heat_clr = _g if heat < 3 else (_y if heat < 4.5 else _r)
        print(heat_clr(f"  Portfolio heat: {heat:.1f}% / 5.0%  "
                       f"({portfolio['position_count']} open positions)"))

    if near_misses:
        _ln()
        nm_str = "  ·  ".join(r.ticker for r in near_misses[:3])
        print(_d(f"  Near misses: {nm_str}"))
        print(_d( "  (run --debug for full breakdown)"))

    _ln()
    print(_d("  ┌─ Commands ──────────────────────────────────────┐"))
    print(_d("  │  python run.py --record TICKER   log a trade    │"))
    print(_d("  │  python run.py --resolve         update P&L     │"))
    print(_d("  │  python run.py --report          analytics      │"))
    print(_d("  │  python run.py --debug           why it failed  │"))
    print(_d("  │  └──────────────────────────────────────────────────┘"))
    _ln()


# ── MAIN ENTRY POINT ───────────────────────────────────────────────────────────

def run_daily_mode(journal_dir: str = "journal") -> None:
    """python run.py --today"""
    try:
        from config.config        import CONFIG
        from analytics.scan_logger import log_scan_results
        from scanner.scan_result  import (
            run_observable_scan, get_near_misses, get_breadth_funnel
        )
        from scanner.portfolio    import load_portfolio_state, filter_by_portfolio
    except ModuleNotFoundError:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from config.config        import CONFIG
        from analytics.scan_logger import log_scan_results
        from scanner.scan_result  import (
            run_observable_scan, get_near_misses, get_breadth_funnel
        )
        from scanner.portfolio    import load_portfolio_state, filter_by_portfolio

    print(f"\n  {_d('Scanning...')}  ", end="\r", flush=True)

    # Load portfolio state FIRST — passes open_risk_inr to scanner
    portfolio = load_portfolio_state(journal_dir)

    regime, results = run_observable_scan(
        CONFIG, open_risk_inr=portfolio["open_risk_inr"]
    )
    funnel      = get_breadth_funnel(results)
    near_misses = get_near_misses(results, n=5)

    all_plans   = [r.plan for r in results if r.passed and r.plan]

    # Inject setup_score, exec_score, regime_score and caution_notes to plans
    result_map = {r.ticker: r for r in results if r.passed}
    for plan in all_plans:
        r = result_map.get(plan["ticker"])
        if r:
            plan.setdefault("caution_notes", r.caution_notes)

    # Filter actionable: not AVOID, not WATCH tier, status not AVOID
    def _actionable(p):
        return (
            p["tier"] not in ("AVOID", "WATCH")
            and p["status"] not in ("AVOID",)
            and p["rr_t1"] >= CONFIG["min_rr_ratio"]
        )

    tier_order   = {"TIER_1": 0, "TIER_2": 1, "PILOT": 2}
    status_order = {"READY": 0, "WATCH": 1, "EXTENDED": 2, "GAPPED": 3}

    actionable = [p for p in all_plans if _actionable(p)]

    # Apply portfolio filters (annotates, doesn't remove)
    actionable = filter_by_portfolio(actionable, portfolio, CONFIG)

    actionable.sort(key=lambda p: (
        status_order.get(p["status"], 9),
        tier_order.get(p["tier"], 9),
        -p["setup_score"],
    ))

    # ── Live overlay: replace EOD close with live LTP during market hours ──
    live_map = {}
    try:
        from scanner.live_data import enrich_with_live
        live_map = enrich_with_live([p["ticker"] for p in actionable])
    except Exception:
        live_map = {}
    for p in actionable:
        snap = live_map.get(p["ticker"], {})
        if snap and snap.get("ltp", 0) > 0:
            p["current_close"] = float(snap["ltp"])
            p["live_change_pct"] = float(snap.get("change_pct", 0.0))
            p["price_src"] = "LIVE" if snap.get("source") == "kite" else "EOD"
        else:
            p["price_src"] = "EOD"

    # ── Plan staleness: how many days has each setup been on the radar? ────
    #   Looks back through the scan log to find when each ticker first
    #   appeared as a READY/WATCH setup. Helps you avoid placing orders
    #   on plans whose entry/stop levels are days old and probably stale.
    try:
        from analytics.scan_logger import load_scan_log as _load_scan
        prior = _load_scan(journal_dir)
        if not prior.empty and "scan_date" in prior.columns:
            actionable_rows = prior[prior["status"].astype(str).isin(["READY", "WATCH"])]
            first_seen = (actionable_rows.groupby("ticker")["scan_date"]
                                          .min().to_dict())
            today_d = date.today()
            for p in actionable:
                fs = first_seen.get(p["ticker"])
                if fs:
                    try:
                        fs_d = datetime.strptime(str(fs)[:10], "%Y-%m-%d").date()
                        p["days_on_radar"] = max(0, (today_d - fs_d).days)
                    except Exception:
                        p["days_on_radar"] = 0
                else:
                    p["days_on_radar"] = 0
    except Exception:
        for p in actionable:
            p["days_on_radar"] = 0

    # Log scan
    if all_plans:
        log_scan_results(all_plans, regime, journal_dir=journal_dir)

    open_positions = _get_open_positions(journal_dir)
    print(" " * 60, end="\r")

    # ── Sector tagging on each plan + open positions ────────────────────────
    smap_local = CONFIG.get("sector_map", {})
    open_sector_counts = {}
    for op in open_positions:
        sec = smap_local.get(op.get("ticker", ""), "OTHER")
        open_sector_counts[sec] = open_sector_counts.get(sec, 0) + 1
    for p in actionable:
        p["sector"] = smap_local.get(p["ticker"], "OTHER")
        already = open_sector_counts.get(p["sector"], 0)
        p["sector_open_count"] = already
        p["sector_crowded"] = already >= 2   # 2+ already open = crowded

    if not actionable:
        _print_no_trade(regime, funnel, portfolio, near_misses)
        if open_positions:
            _print_open_positions(open_positions)
        return

    _print_header(regime, funnel, portfolio)

    if open_positions:
        _print_open_positions(open_positions)

    _print_overview_table(actionable)
    _print_sector_breakdown(actionable, open_sector_counts)

    _ln()
    print(_d("─" * W))
    print(_d("  EXECUTION CARDS"))
    print(_d("─" * W))

    # Regime gate banner — shown once above all cards
    _cur_regime = regime.get("regime", "BULL")
    if _cur_regime == "BEAR":
        _ln()
        print(_r("  " + "═" * (W - 2)))
        print(_r("  ⚠  BEAR REGIME  —  NEW ENTRIES GATED"))
        print(_r("  " + "─" * (W - 2)))
        print(_r("  NIFTY is below key SMAs. --place will require"))
        print(_r("  manual override. Setups below are shown for"))
        print(_r("  planning only. Do not trade into a downtrend."))
        print(_r("  " + "═" * (W - 2)))
    elif _cur_regime == "NEUTRAL":
        _ln()
        print(_y("  ⚠  NEUTRAL REGIME  —  raise your bar today."))
        print(_y("    Only the highest-score setups with clear RS. Max 1 new trade."))

    for i, plan in enumerate(actionable, 1):
        _ln()
        _print_card(i, plan, regime["regime"])

    n_ready = sum(1 for p in actionable if p["status"] == "READY")
    n_watch = sum(1 for p in actionable if p["status"] == "WATCH")
    _print_footer(n_ready, n_watch, near_misses, portfolio, journal_dir)

    # Alert engine: surface WATCH→READY transitions, stale setups, and
    # any READY that a pre-trade guard would reject right now.
    try:
        from scanner.alerts import run_alerts
        run_alerts(journal_dir)
    except Exception as _e:
        # Alerts are advisory — never let them break the cockpit
        print(f"  (alerts skipped: {_e})")

    # ── Telegram morning briefing ──────────────────────────────────────────
    try:
        from integrations.telegram_notifier import notify_morning_briefing
        from config.config import CONFIG
        cap = float(CONFIG.get("account_capital", 100_000))
        deployed = portfolio.get("deployed_pct", 0.0) if portfolio else 0.0
        ready_list = [
            {"ticker": p["ticker"],
             "score": p.get("exec_score", p.get("score", "")),
             "entry": p.get("entry_price", 0),
             "sector": p.get("sector", "")}
            for p in actionable if p.get("status") == "READY"
        ]
        notify_morning_briefing(
            ready=ready_list, watch_count=n_watch,
            capital=cap, deployed_pct=deployed,
        )
    except Exception:
        pass

    # ── Telegram pre-market regime alert ──────────────────────────────────
    try:
        from integrations.telegram_notifier import notify_premarket_regime
        from config.config import CONFIG
        idx_df       = regime.get("index_df")
        nifty_price  = 0.0
        nifty_chg    = 0.0
        above_20sma  = True
        above_50sma  = True
        if idx_df is not None and not idx_df.empty:
            close       = float(idx_df["Close"].iloc[-1])
            prev_close  = float(idx_df["Close"].iloc[-2]) if len(idx_df) > 1 else close
            nifty_price = close
            nifty_chg   = round((close - prev_close) / prev_close * 100, 2) if prev_close else 0
            if "SMA_fast" in idx_df.columns:
                above_20sma = close > float(idx_df["SMA_fast"].iloc[-1])
            if "SMA_slow" in idx_df.columns:
                above_50sma = close > float(idx_df["SMA_slow"].iloc[-1])
        notify_premarket_regime(
            regime       = regime["regime"],
            nifty_price  = nifty_price,
            nifty_chg_pct= nifty_chg,
            above_20sma  = above_20sma,
            above_50sma  = above_50sma,
            ready_count  = n_ready,
            max_positions= int(CONFIG.get("max_positions", 3)),
        )
    except Exception:
        pass