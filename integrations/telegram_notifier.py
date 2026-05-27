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
