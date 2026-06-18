"""
scanner/positions.py — Active Position Lifecycle Manager
=========================================================
Command:  python run.py --positions

Single source of truth for everything that happens AFTER entry:
  - Current R-multiple, unrealized P&L (₹ and R)
  - Holding duration
  - Recommended stop  (breakeven shift after T1, ATR trailing)
  - Failed breakout detection  (early & deep red)
  - Time-stop alerts  (held too long, no progress)
  - One next-action recommendation per position

`daily_mode.py` also consumes load_active_positions() so the
cockpit and the dedicated --positions view never disagree.
"""

import os
import sys
from datetime import date, datetime
from typing import Optional

import pandas as pd
import yfinance as yf

try:
    from analytics.outcome_tracker import load_trade_log
    from config.config             import CONFIG
except ModuleNotFoundError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from analytics.outcome_tracker import load_trade_log
    from config.config             import CONFIG


# ── Colour helpers ────────────────────────────────────────────────────────────
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


# ═══════════════════════════════════════════════════════════════
# DATA FETCH
# ═══════════════════════════════════════════════════════════════

def _fetch_history(ticker: str, start: str) -> Optional[pd.DataFrame]:
    """OHLC from entry_date → today.  None if data unavailable."""
    try:
        df = yf.download(ticker, start=start, auto_adjust=True, progress=False)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna()


def _atr(df: pd.DataFrame, window: int = 14) -> float:
    """Most recent ATR value."""
    if len(df) < 2:
        return 0.0
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr_s = tr.rolling(window).mean()
    val = atr_s.iloc[-1]
    return 0.0 if pd.isna(val) else float(val)


# ═══════════════════════════════════════════════════════════════
# LIFECYCLE LOGIC  —  one function, one source of truth
# ═══════════════════════════════════════════════════════════════

def _lifecycle(pos: dict, config: dict) -> dict:
    """
    Annotate pos with: t1_touched, stop_recommended, stop_reason,
    failed_breakout, time_stop_due, action, action_priority.
    """
    entry        = pos["entry"]
    stop         = pos["stop"]
    t1           = pos["t1"]
    atr          = pos["atr"]
    current      = pos["current"]
    highest_high = pos["highest_high"]
    days_held    = pos["days_held"]
    r_current    = pos["r_current"]

    trail_mult   = config.get("trailing_atr_multiple", 2.0)
    time_stop_d  = config.get("time_stop_days",        20)
    fb_days      = config.get("failed_breakout_days",  5)
    fb_r         = config.get("failed_breakout_r",    -0.5)

    # T1 was touched at any point during the trade?
    t1_touched = bool(t1) and highest_high >= t1

    # Pick the highest (most protective) candidate stop.
    candidates = [(stop, "original")]
    if t1_touched:
        candidates.append((entry, "breakeven (T1 was touched)"))
    if atr > 0 and r_current >= 1.0:
        trail = round(current - trail_mult * atr, 2)
        if trail > stop:
            candidates.append((trail, f"ATR trail (Close − {trail_mult:g}×ATR)"))
    stop_recommended, stop_reason = max(candidates, key=lambda c: c[0])

    failed_breakout = (
        days_held <= fb_days
        and r_current <= fb_r
        and current < entry
    )
    time_stop_due = days_held >= time_stop_d and r_current < 1.0

    # Single recommended action — most urgent wins (lower priority = more urgent)
    if r_current <= -0.95:
        action, priority = "STOP LIKELY HIT — verify and exit now", 0
    elif failed_breakout:
        action, priority = (
            f"FAILED BREAKOUT — exit at market ({days_held}d in, {r_current:+.1f}R)",
            0,
        )
    elif time_stop_due:
        action, priority = (
            f"TIME STOP — {days_held}d held with no progress, exit at open",
            1,
        )
    elif r_current >= 2.0 and t1_touched:
        action, priority = "BOOK PARTIAL at T1 if not done, trail the rest", 2
    elif stop_recommended > stop:
        action, priority = (
            f"RAISE STOP to ₹{stop_recommended:,.2f}  ({stop_reason})",
            3,
        )
    elif r_current >= 0.5:
        action, priority = "Hold — plan intact, monitor trail", 4
    elif r_current >= 0:
        action, priority = "Hold — early in trade", 5
    else:
        action, priority = "Monitor — approaching stop", 4

    pos.update({
        "t1_touched":       t1_touched,
        "stop_recommended": stop_recommended,
        "stop_reason":      stop_reason,
        "failed_breakout":  failed_breakout,
        "time_stop_due":    time_stop_due,
        "action":           action,
        "action_priority":  priority,
    })
    return pos


# ═══════════════════════════════════════════════════════════════
# LOADER
# ═══════════════════════════════════════════════════════════════

def load_active_positions(journal_dir: str = "journal",
                           config: dict = CONFIG) -> list[dict]:
    """Read open trades, enrich with live price + lifecycle decisions."""
    df = load_trade_log(journal_dir)
    if df.empty:
        return []

    open_mask = (
        (df["resolved"].astype(str).str.upper() != "TRUE") &
        (df["exit_reason"].astype(str).isin(["OPEN", "", "nan"]))
    )
    open_df = df[open_mask].copy()
    if open_df.empty:
        return []

    today = pd.Timestamp(date.today())

    # ── Single batched live-price fetch (Kite when market open, else yf) ──
    try:
        from scanner.live_data import enrich_with_live
        live = enrich_with_live(open_df["ticker"].astype(str).tolist())
    except Exception:
        live = {}

    positions = []

    for _, row in open_df.iterrows():
        ticker = str(row.get("ticker", ""))
        entry  = float(row.get("entry_price", 0) or 0)
        stop   = float(row.get("stop_price",  0) or 0)
        t1     = float(row.get("t1", 0) or 0)
        t2     = float(row.get("t2", 0) or 0)
        qty    = int(float(row.get("quantity", 0) or 0))
        risk   = entry - stop
        if not ticker or entry <= 0 or risk <= 0:
            continue

        entry_dt = row.get("entry_date")
        if not isinstance(entry_dt, pd.Timestamp):
            try:
                entry_dt = pd.Timestamp(entry_dt)
            except Exception:
                entry_dt = today
        entry_date_str = entry_dt.strftime("%Y-%m-%d")

        hist = _fetch_history(ticker, entry_date_str)
        if hist is None or hist.empty:
            current      = entry
            highest_high = entry
            lowest_low   = entry
            atr_val      = 0.0
        else:
            current      = float(hist["Close"].iloc[-1])
            highest_high = float(hist["High"].max())
            lowest_low   = float(hist["Low"].min())
            atr_val      = _atr(hist)

        # Live overlay — Kite LTP (intraday) takes precedence over yf close
        live_snap   = live.get(ticker, {}) if live else {}
        price_src   = "EOD"
        if live_snap and live_snap.get("ltp", 0) > 0:
            current   = float(live_snap["ltp"])
            price_src = "LIVE" if live_snap.get("source") == "kite" else "EOD"
            # Update highs/lows with live intraday extremes if present
            dh = float(live_snap.get("day_high", 0) or 0)
            dl = float(live_snap.get("day_low",  0) or 0)
            if dh > highest_high: highest_high = dh
            if dl and (lowest_low == entry or dl < lowest_low): lowest_low = dl

        days_held      = max(1, int((today - entry_dt).days))
        r_current      = (current - entry) / risk if risk else 0.0
        unrealized_inr = (current - entry) * qty
        dist_to_t1     = ((t1 - current) / current * 100) if current and t1 else 0.0

        pos = {
            "ticker":         ticker,
            "entry_date":     entry_date_str,
            "days_held":      days_held,
            "entry":          entry,
            "stop":           stop,
            "t1":             t1,
            "t2":             t2,
            "quantity":       qty,
            "risk_per_share": risk,
            "current":        current,
            "highest_high":   highest_high,
            "lowest_low":     lowest_low,
            "atr":            atr_val,
            "r_current":      round(r_current, 2),
            "unrealized_inr": round(unrealized_inr, 0),
            "mfe_pct":        round(((highest_high - entry) / entry) * 100, 2) if entry else 0.0,
            "mae_pct":        round(((lowest_low  - entry) / entry) * 100, 2) if entry else 0.0,
            "dist_to_t1":     round(dist_to_t1, 1),
            "grade":          str(row.get("grade", "")),
            "price_src":      price_src,
        }
        positions.append(_lifecycle(pos, config))

    # Sort by urgency, then by R (worst-positioned first within same urgency)
    positions.sort(key=lambda p: (p["action_priority"], -p["r_current"]))
    return positions


# ═══════════════════════════════════════════════════════════════
# DISPLAY  —  --positions cockpit
# ═══════════════════════════════════════════════════════════════

W = 62
def _rule(): print("═" * W)
def _thin(): print("─" * W)
def _ln():   print()


def _action_clr(priority: int):
    return _r if priority <= 1 else (_y if priority <= 3 else _g)


def print_dashboard(positions: list[dict], journal_dir: str = "journal") -> None:
    """Full --positions cockpit."""
    try:
        from scanner.portfolio import load_portfolio_state
    except ModuleNotFoundError:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from scanner.portfolio import load_portfolio_state

    portfolio = load_portfolio_state(journal_dir)
    ts        = datetime.now().strftime("%d %b %Y  %H:%M")

    # Data freshness banner — driven by whatever load_active_positions used
    if positions:
        live_count = sum(1 for p in positions if p.get("price_src") == "LIVE")
        if live_count == len(positions):
            src_badge = _g("LIVE")
        elif live_count > 0:
            src_badge = _y(f"LIVE {live_count}/{len(positions)}")
        else:
            src_badge = _d("EOD")
    else:
        src_badge = _d("EOD")

    _rule()
    print(f"  {_b('ACTIVE POSITIONS')}  —  {_d(ts)}   {src_badge}")
    _rule()
    _ln()

    if not positions:
        print(f"  {_d('No open positions. Run --today to find new setups.')}")
        _ln()
        return

    total_inr = sum(p["unrealized_inr"] for p in positions)
    total_r   = sum(p["r_current"]      for p in positions)

    heat     = portfolio["heat_used_pct"]
    heat_clr = _g if heat < 3.0 else (_y if heat < 4.5 else _r)

    inr_clr = _g if total_inr >= 0 else _r
    r_clr   = _g if total_r   >= 0 else _r
    inr_txt = f"₹{total_inr:+,.0f}"
    r_txt   = f"{total_r:+.1f}R"

    print(f"  {_b('Open:')} {len(positions)}   "
          f"{_b('Heat:')} {heat_clr(f'{heat:.1f}%')}/5.0%   "
          f"{_b('Unrealized:')} {inr_clr(inr_txt)}  ({r_clr(r_txt)})")
    _thin()
    _ln()

    for p in positions:
        _print_card(p)

    # Bottom-line summary: how many need action today
    urgent = sum(1 for p in positions if p["action_priority"] <= 1)
    raises = sum(1 for p in positions if p["action_priority"] == 3)
    booked = sum(1 for p in positions if p["action_priority"] == 2)

    _rule()
    if urgent:
        print(f"  {_r(f'  {urgent} URGENT — exit today')}")
    if booked:
        print(f"  {_y(f'  {booked} ready to book partial at T1')}")
    if raises:
        print(f"  {_y(f'  {raises} should raise stop')}")
    if not (urgent or raises or booked):
        print(f"  {_g('  All positions on plan. No action required today.')}")
    _ln()


def _print_card(p: dict) -> None:
    ticker     = p["ticker"]
    r          = p["r_current"]
    days       = p["days_held"]
    rclr       = _g if r >= 1.0 else (_y if r >= 0 else _r)
    aclr       = _action_clr(p["action_priority"])

    badges = []
    if p["t1_touched"]:      badges.append(_g("T1✓"))
    if p["failed_breakout"]: badges.append(_r("FAILED BREAKOUT"))
    if p["time_stop_due"]:   badges.append(_y("TIME STOP"))
    badge_str = "  ".join(badges)

    header_r  = rclr(f"{r:+.2f}R")
    header_dy = _d(f"{days}d held")
    print(f"  {_c(_b(ticker)):<14}  {header_r}   {header_dy}   {badge_str}")
    _thin()

    print(f"    Entry ₹{p['entry']:>9,.2f}   Now  ₹{p['current']:>9,.2f}   "
          f"Stop ₹{p['stop']:>9,.2f}")
    print(f"    T1    ₹{p['t1']:>9,.2f}   T2   ₹{p['t2']:>9,.2f}   "
          f"ATR  {p['atr']:>9,.2f}")
    print(f"    MFE   {p['mfe_pct']:>+8.1f}%   MAE  {p['mae_pct']:>+8.1f}%   "
          f"Qty  {p['quantity']:>9,}")

    unreal  = p["unrealized_inr"]
    uclr    = _g if unreal >= 0 else _r
    unr_txt = f"₹{unreal:+,.0f}"
    r_txt   = f"{r:+.2f}R"
    print(f"    {_b('Unrealized:')} {uclr(unr_txt)}   ({uclr(r_txt)})")

    if p["stop_recommended"] > p["stop"]:
        rec_txt = f"₹{p['stop_recommended']:,.2f}"
        print(f"    {_b('Recommended stop:')} {_g(rec_txt)}  "
              f"{_d('— ' + p['stop_reason'])}")

    _ln()
    print(f"    {aclr('→  ' + p['action'])}")
    _ln()


# ═══════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def run_positions_mode(journal_dir: str = "journal", quiet: bool = False) -> None:
    """python run.py --positions

    When `quiet=True` (used by the scheduled task), suppress the dashboard
    if there are no auto-fills and no urgent actions — keeps the log clean.
    """
    # Auto-detect any filled buy orders from Kite that aren't in the journal
    auto_n = 0
    try:
        from scanner.trade_placement import detect_and_record_fills
        auto_n = detect_and_record_fills(journal_dir)
        if auto_n:
            print(_d(f"  ({auto_n} auto-recorded fill{'s' if auto_n != 1 else ''} above)\n"))
    except Exception:
        pass

    if not quiet:
        print(f"\n  {_d('Loading positions...')}  ", end="\r", flush=True)
    positions = load_active_positions(journal_dir, CONFIG)
    if not quiet:
        print(" " * 60, end="\r")

    if quiet:
        # Only print dashboard when something happened or needs attention
        urgent = sum(1 for p in positions if p.get("action_priority") in ("URGENT",))
        if auto_n == 0 and urgent == 0:
            return  # silent — nothing to report

    print_dashboard(positions, journal_dir)

    # ── Telegram: R milestones + unprotected positions + heat ─────────────
    try:
        from integrations.telegram_notifier import (
            check_and_notify_r_milestones,
            check_and_notify_unprotected,
            check_and_notify_heat,
        )
        cap        = float(CONFIG.get("account_capital", 100_000))
        max_heat   = float(CONFIG.get("max_portfolio_heat", 0.03)) * 100
        risk_inr   = sum(
            float(p.get("risk_per_share", 0)) * int(p.get("quantity", 0))
            for p in positions
        )
        heat_pct   = round(risk_inr / cap * 100, 2) if cap else 0.0

        check_and_notify_r_milestones(positions, journal_dir)
        check_and_notify_unprotected(positions, journal_dir)
        check_and_notify_heat(
            heat_pct=heat_pct, max_heat_pct=max_heat,
            open_count=len(positions), capital=cap,
            journal_dir=journal_dir,
        )
    except Exception:
        pass


