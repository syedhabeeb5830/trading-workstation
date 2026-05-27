"""
integrations/telegram_notifier.py
──────────────────────────────────
Thin wrapper around the Telegram Bot HTTP API.
All public functions are fire-and-forget: they never raise — the
trading workflow must never break because of a notification failure.

Environment variables (loaded from .env):
    TELEGRAM_BOT_TOKEN   — BotFather token
    TELEGRAM_CHAT_ID     — your personal chat ID

Usage:
    from integrations.telegram_notifier import notify_fill, notify_sl_hit, ...
"""

from __future__ import annotations

import os
import threading
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv optional; fall back to os.environ

# ── Config ────────────────────────────────────────────────────────────────────

_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
_BASE    = "https://api.telegram.org/bot"
_TIMEOUT = 8  # seconds


def _enabled() -> bool:
    return bool(_TOKEN and _CHAT_ID)


# ── Core send (background thread so it never blocks the main flow) ─────────────

def _send(text: str) -> None:
    """Non-blocking send: spawns a non-daemon thread so short-lived scripts
    don't exit before the HTTP call completes (max _TIMEOUT seconds)."""
    if not _enabled():
        return
    def _post() -> None:
        try:
            import requests  # lazy import — already in requirements
            requests.post(
                f"{_BASE}{_TOKEN}/sendMessage",
                json={"chat_id": _CHAT_ID, "text": text,
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=_TIMEOUT,
            )
        except Exception:
            pass  # silent — notification failure must not crash the workstation
    # daemon=False: the thread keeps the process alive until the send completes
    threading.Thread(target=_post, daemon=False).start()


# ── Trading event notifications ───────────────────────────────────────────────

def notify_fill(
    ticker: str,
    fill_price: float,
    qty: int,
    stop: float,
    target1: float,
    risk_inr: float,
    portfolio_heat_pct: float,
    gtt_placed: bool = True,
) -> None:
    """Called when a buy-stop GTT fills and the entry is auto-recorded."""
    gtt_line = "✅ Protective GTT placed (SL + T1)" if gtt_placed else \
               "⚠️ Protective GTT FAILED — set manual stop on Kite"
    _send(
        f"✅ <b>FILL: {ticker}</b>\n"
        f"Entry ₹{fill_price:,.2f} × {qty} shares\n"
        f"Stop ₹{stop:,.2f}  |  T1 ₹{target1:,.2f}\n"
        f"Risk: ₹{risk_inr:,.0f}  ({portfolio_heat_pct:.2f}% of capital)\n"
        f"{gtt_line}"
    )


def notify_sl_hit(
    ticker: str,
    exit_price: float,
    r_multiple: float,
    loss_inr: float,
    open_trades: int,
) -> None:
    """Called when a stop-loss is detected (position gone, exit recorded)."""
    _send(
        f"🔴 <b>SL HIT: {ticker}</b>\n"
        f"Exit ₹{exit_price:,.2f}  |  {r_multiple:.1f}R\n"
        f"Loss: ₹{loss_inr:,.0f}\n"
        f"Open trades remaining: {open_trades}"
    )


def notify_target_hit(
    ticker: str,
    exit_price: float,
    r_multiple: float,
    profit_inr: float,
    open_trades: int,
) -> None:
    """Called when a target is detected (position closed at profit)."""
    _send(
        f"🎯 <b>TARGET HIT: {ticker}</b>\n"
        f"Exit ₹{exit_price:,.2f}  |  +{r_multiple:.1f}R\n"
        f"Profit: ₹{profit_inr:,.0f}\n"
        f"Open trades remaining: {open_trades}"
    )


def notify_stop_trailed(
    ticker: str,
    old_stop: float,
    new_stop: float,
    locked_r: float,
) -> None:
    """Called after --trail successfully modifies the GTT."""
    _send(
        f"🔒 <b>STOP TRAILED: {ticker}</b>\n"
        f"₹{old_stop:,.2f}  →  ₹{new_stop:,.2f}\n"
        f"Locked in: {locked_r:.2f}R"
    )


def notify_partial_exit(
    ticker: str,
    qty: int,
    price: float,
    partial_r: float,
    new_stop: float,
) -> None:
    """Called after --partial records the exit and moves stop to breakeven."""
    _send(
        f"📦 <b>PARTIAL EXIT: {ticker}</b>\n"
        f"Sold {qty} shares at ₹{price:,.2f}  (+{partial_r:.2f}R)\n"
        f"Stop moved to breakeven ₹{new_stop:,.2f}\n"
        f"⚠️ Remember to sell {qty} shares on Kite manually"
    )


# ── Daily briefings ───────────────────────────────────────────────────────────

def notify_morning_briefing(
    ready: list[dict],
    watch_count: int,
    capital: float,
    deployed_pct: float,
) -> None:
    """
    Called at the end of --today.
    ready: list of dicts with keys: ticker, score, entry, stop, sector
    """
    if not ready:
        _send(
            "🌅 <b>Morning Scan</b>\n"
            f"No READY setups today.\n"
            f"{watch_count} on WATCH.\n"
            f"Capital: ₹{capital:,.0f}  |  Deployed: {deployed_pct:.1f}%"
        )
        return

    top = ready[:3]  # top 3 by score
    lines = [f"🌅 <b>Morning Scan — {len(ready)} READY</b>"]
    for i, s in enumerate(top, 1):
        lines.append(
            f"{i}. {s['ticker']}  score {s.get('score', '—')}  "
            f"entry ₹{s.get('entry', 0):,.2f}  [{s.get('sector', '')}]"
        )
    if len(ready) > 3:
        lines.append(f"…+{len(ready)-3} more (run --today)")
    lines.append(f"\n{watch_count} on WATCH  |  Deployed: {deployed_pct:.1f}%")
    _send("\n".join(lines))


def notify_evening_summary(
    open_positions: list[dict],
    capital: float,
    total_heat_pct: float,
) -> None:
    """
    Called at the end of --positions at/after 15:30.
    open_positions: list of dicts with ticker, current_r, status
    """
    if not open_positions:
        _send(
            "🌆 <b>EOD Summary</b>\n"
            "No open positions.\n"
            f"Capital: ₹{capital:,.0f}  |  Heat: {total_heat_pct:.1f}%"
        )
        return

    lines = [f"🌆 <b>EOD Summary — {len(open_positions)} open</b>"]
    for p in open_positions:
        r = p.get("current_r", 0.0)
        emoji = "🟢" if r > 0 else "🔴" if r < -0.5 else "🟡"
        lines.append(f"{emoji} {p['ticker']}  {r:+.2f}R")
    lines.append(f"\nTotal heat: {total_heat_pct:.1f}%  |  Capital: ₹{capital:,.0f}")
    _send("\n".join(lines))


def notify_raw(message: str) -> None:
    """Send any freeform message. Use for one-off alerts."""
    _send(message)


# ── Notification state (dedup / rate-limit) ───────────────────────────────────
# Stored in journal/telegram_state.json so it survives process restarts.

import json as _json
from datetime import datetime as _dt, date as _date

_STATE_FILE_NAME = "telegram_state.json"


def _load_state(journal_dir: str) -> dict:
    p = os.path.join(journal_dir, _STATE_FILE_NAME)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return _json.load(f)
    except Exception:
        return {}


def _save_state(journal_dir: str, state: dict) -> None:
    p = os.path.join(journal_dir, _STATE_FILE_NAME)
    os.makedirs(journal_dir, exist_ok=True)
    try:
        with open(p, "w", encoding="utf-8") as f:
            _json.dump(state, f, indent=2)
    except Exception:
        pass


# ── R-milestone nudge ─────────────────────────────────────────────────────────
# Fires once per milestone per trade. Milestones: +1R, +1.5R, +2R, +3R.

_R_MILESTONES = [1.0, 1.5, 2.0, 3.0]


def check_and_notify_r_milestones(positions: list[dict],
                                   journal_dir: str = "journal") -> None:
    """
    Called every --positions run.
    Sends one Telegram message the FIRST TIME a trade crosses each R level.
    """
    state = _load_state(journal_dir)
    sent  = state.setdefault("r_milestones", {})  # {trade_key: [1.0, 2.0, ...]}
    dirty = False

    for p in positions:
        ticker   = p.get("ticker", "")
        r        = float(p.get("r_current", 0.0))
        entry    = float(p.get("entry",  0))
        current  = float(p.get("current", 0))
        t1       = float(p.get("t1",  0))
        t2       = float(p.get("t2",  0))
        risk     = float(p.get("risk_per_share", 0))
        gain_inr = float(p.get("unrealized_inr", 0))
        key      = f"{ticker}_{p.get('entry_date','')}"
        fired    = sent.get(key, [])

        for m in _R_MILESTONES:
            if r >= m and m not in fired:
                fired.append(m)
                dirty = True
                # Build suggested action based on milestone
                if m == 1.0:
                    action = (
                        f"→ Book partial: sell half position\n"
                        f"→ Or trail stop above entry to lock in 0R"
                    )
                elif m == 1.5:
                    action = f"→ Trail stop to +0.5R if not already done"
                elif m == 2.0:
                    action = (
                        f"→ If T1 not booked yet: sell half now\n"
                        f"→ Trail stop to +1R on remainder"
                    )
                else:
                    action = f"→ Review: is T2 still realistic? Consider full exit."

                dist_t2 = f"₹{t2:,.2f}" if t2 else "—"
                _send(
                    f"📈 <b>+{m:.1f}R MILESTONE  —  {ticker}</b>\n"
                    f"{'─' * 28}\n"
                    f"Entry     ₹{entry:>10,.2f}\n"
                    f"Now       ₹{current:>10,.2f}\n"
                    f"Gain      ₹{gain_inr:>+10,.0f}  (+{r:.2f}R)\n"
                    f"T2        {dist_t2:>11}\n"
                    f"{'─' * 28}\n"
                    f"{action}"
                )

        sent[key] = fired

    if dirty:
        state["r_milestones"] = sent
        _save_state(journal_dir, state)


# ── Unprotected position warning ──────────────────────────────────────────────
# Fires at most once per hour per ticker.

def check_and_notify_unprotected(positions: list[dict],
                                  journal_dir: str = "journal") -> None:
    """
    Called every --positions run.
    Warns if an open trade has no ACTIVE protective GTT in gtt_orders.csv.
    Rate-limited: at most one alert per ticker per hour.
    """
    if not positions:
        return
    try:
        import pandas as pd
        from pathlib import Path
        gtt_path = Path(journal_dir) / "gtt_orders.csv"
        if gtt_path.exists():
            gtt_df = pd.read_csv(gtt_path)
            protected = set(
                gtt_df.loc[
                    (gtt_df["gtt_kind"].astype(str) == "PROTECTIVE") &
                    (gtt_df["status"].astype(str) == "ACTIVE"),
                    "ticker"
                ].tolist()
            )
        else:
            protected = set()
    except Exception:
        protected = set()

    state     = _load_state(journal_dir)
    last_sent = state.setdefault("unprotected_last_sent", {})
    dirty     = False
    now_str   = _dt.now().strftime("%Y-%m-%dT%H")  # hour-level key

    for p in positions:
        ticker = p.get("ticker", "")
        if ticker in protected:
            continue
        if last_sent.get(ticker) == now_str:
            continue  # already alerted this hour

        stop    = float(p.get("stop", 0))
        entry   = float(p.get("entry", 0))
        current = float(p.get("current", 0))
        r       = float(p.get("r_current", 0))

        _send(
            f"🚨 <b>POSITION UNPROTECTED  —  {ticker}</b>\n"
            f"{'─' * 28}\n"
            f"Entry    ₹{entry:>10,.2f}\n"
            f"Now      ₹{current:>10,.2f}  ({r:+.2f}R)\n"
            f"SL level ₹{stop:>10,.2f}\n"
            f"{'─' * 28}\n"
            f"No active GTT on Zerodha.\n"
            f"Place stop manually on Kite NOW\n"
            f"or run --positions to auto-fix."
        )
        last_sent[ticker] = now_str
        dirty = True

    if dirty:
        state["unprotected_last_sent"] = last_sent
        _save_state(journal_dir, state)


# ── Portfolio heat warning ─────────────────────────────────────────────────────
# Fires at most once per calendar day.

def check_and_notify_heat(heat_pct: float, max_heat_pct: float,
                           open_count: int, capital: float,
                           journal_dir: str = "journal") -> None:
    """
    Called every --positions run.
    Fires when total portfolio heat exceeds 80% of the configured limit.
    Rate-limited to once per day.
    """
    threshold = max_heat_pct * 80  # 80% of max e.g. 2.4% of 3%
    if heat_pct < threshold:
        return

    state      = _load_state(journal_dir)
    today_str  = str(_date.today())
    if state.get("heat_warning_date") == today_str:
        return  # already warned today

    _send(
        f"⚠️ <b>PORTFOLIO HEAT WARNING</b>\n"
        f"{'─' * 28}\n"
        f"Current heat   {heat_pct:.1f}%\n"
        f"Your limit     {max_heat_pct:.1f}%\n"
        f"Open trades    {open_count}\n"
        f"Capital at risk  ₹{capital * heat_pct / 100:,.0f}\n"
        f"{'─' * 28}\n"
        f"Do not open new positions\n"
        f"until an existing one closes."
    )
    state["heat_warning_date"] = today_str
    _save_state(journal_dir, state)


# ── Pre-market regime alert ───────────────────────────────────────────────────
# Called from --today. No rate-limit — user runs --today once a morning.

def notify_premarket_regime(
    regime: str,
    nifty_price: float,
    nifty_chg_pct: float,
    above_20sma: bool,
    above_50sma: bool,
    ready_count: int,
    max_positions: int,
) -> None:
    emoji   = {"BULL": "🟢", "NEUTRAL": "🟡", "CAUTION": "🟠", "BEAR": "🔴"}.get(regime, "🟡")
    sma20   = "✓ Above 20 SMA" if above_20sma else "✗ Below 20 SMA"
    sma50   = "✓ Above 50 SMA" if above_50sma else "✗ Below 50 SMA"
    chg     = f"{nifty_chg_pct:+.2f}%"

    if regime == "BULL":
        guidance = f"Proceed normally. Max {max_positions} new trades."
    elif regime == "NEUTRAL":
        guidance = f"Selective. Max {min(max_positions, 2)} new trades. Higher quality only."
    elif regime == "CAUTION":
        guidance = "Reduce size. Max 1 new trade. Tight stops."
    else:
        guidance = "BEAR regime. No new longs. Protect capital."

    _send(
        f"{emoji} <b>PRE-MARKET  —  {regime}</b>\n"
        f"{'─' * 28}\n"
        f"NIFTY    ₹{nifty_price:>10,.2f}  {chg}\n"
        f"         {sma20}\n"
        f"         {sma50}\n"
        f"{'─' * 28}\n"
        f"READY setups today:  {ready_count}\n"
        f"{guidance}"
    )


# ── Weekly pulse ──────────────────────────────────────────────────────────────
# Called from --report on Sundays (or --weekly-pulse any time).

def notify_weekly_pulse(
    wins: int,
    losses: int,
    net_r: float,
    expectancy_20: float,
    open_count: int,
    capital_at_risk_inr: float,
    best_edge: str = "",
    watch_edge: str = "",
    journal_dir: str = "journal",
) -> None:
    week_num  = _dt.now().isocalendar()[1]
    win_str   = f"{wins}W  {losses}L"
    r_str     = f"{net_r:+.1f}R"
    exp_emoji = "✅" if expectancy_20 >= 0.5 else ("🟡" if expectancy_20 >= 0 else "🔴")

    best_line  = f"\nBest edge    {best_edge}" if best_edge else ""
    watch_line = f"\nWatch        {watch_edge}" if watch_edge else ""

    _send(
        f"📊 <b>WEEKLY PULSE  —  Week {week_num}</b>\n"
        f"{'─' * 28}\n"
        f"This week    {win_str}  |  {r_str}\n"
        f"{'─' * 28}\n"
        f"Rolling 20-trade expectancy\n"
        f"{exp_emoji}  {expectancy_20:+.2f}R per trade\n"
        f"{'─' * 28}\n"
        f"Open now     {open_count} trades\n"
        f"At risk      ₹{capital_at_risk_inr:,.0f}"
        f"{best_line}"
        f"{watch_line}"
    )

    # Mark sent so --report doesn't double-fire on same Sunday
    state = _load_state(journal_dir)
    state["weekly_pulse_last_sent"] = str(_date.today())
    _save_state(journal_dir, state)


def weekly_pulse_already_sent_today(journal_dir: str = "journal") -> bool:
    state = _load_state(journal_dir)
    return state.get("weekly_pulse_last_sent") == str(_date.today())
