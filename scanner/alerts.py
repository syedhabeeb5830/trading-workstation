"""
scanner/alerts.py — Alert Engine
=================================
Pure-signal alerts. No noise. Three categories:

  TRIGGER     A ticker that was WATCH yesterday is READY today.
              "Setup just armed — review entry."

  STALE       An open setup that's been WATCH/READY for > setup_expiry_days
              without you taking the trade. "Either take it or drop it."

  BLOCKED     A would-be READY today, but a guard rejected it.
              (drift / gap-up / sector full / heat cap / etc.)

Sinks:
  - Console (always)
  - journal/alerts.log (always — append-only audit trail)
  - Windows toast via winotify (if installed)

Run via:
  python run.py --alerts
Also runs automatically inside --today.
"""

from __future__ import annotations
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from analytics.scan_logger    import load_scan_log
from analytics.outcome_tracker import load_trade_log
from scanner.guards           import run_all_guards
from config.config            import CONFIG


# ── Colour helpers ───────────────────────────────────────────────────────────
_R, _Y, _G, _C, _D, _B, _RST = (
    "\033[91m", "\033[93m", "\033[92m",
    "\033[96m", "\033[2m",  "\033[1m", "\033[0m",
)
def _r(s): return f"{_R}{s}{_RST}"
def _y(s): return f"{_Y}{s}{_RST}"
def _g(s): return f"{_G}{s}{_RST}"
def _c(s): return f"{_C}{s}{_RST}"
def _b(s): return f"{_B}{s}{_RST}"
def _d(s): return f"{_D}{s}{_RST}"


# ─────────────────────────────────────────────────────────────────────────────
# TOAST (Windows-only, soft dependency)
# ─────────────────────────────────────────────────────────────────────────────
def _toast(title: str, msg: str) -> None:
    try:
        from winotify import Notification  # type: ignore
        Notification(app_id="Trading Workstation",
                     title=title, msg=msg).show()
    except Exception:
        # Library missing or platform unsupported — silent fallback
        pass


# ─────────────────────────────────────────────────────────────────────────────
# AUDIT LOG
# ─────────────────────────────────────────────────────────────────────────────
def _log_alert(journal_dir: str, kind: str, ticker: str, message: str) -> None:
    log_path = Path(journal_dir) / "alerts.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().isoformat(timespec='seconds')}\t{kind}\t{ticker}\t{message}\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)


# ─────────────────────────────────────────────────────────────────────────────
# SCAN-LOG DIFF
# ─────────────────────────────────────────────────────────────────────────────
def _latest_per_ticker_on_date(scan_df: pd.DataFrame, on_date: date) -> pd.DataFrame:
    """For a given scan_date, returns the LAST scan row per ticker."""
    if scan_df.empty:
        return scan_df
    day = scan_df[scan_df["scan_date"].astype(str) == on_date.isoformat()]
    if day.empty:
        return day
    return (day.sort_values("scan_timestamp")
                .drop_duplicates("ticker", keep="last")
                .reset_index(drop=True))


def _detect_watch_to_ready(scan_df: pd.DataFrame, today: date) -> list[dict]:
    """Tickers whose latest status moved from WATCH (yesterday) to READY (today)."""
    today_rows = _latest_per_ticker_on_date(scan_df, today)
    if today_rows.empty:
        return []

    # Look back up to 5 calendar days for the most recent prior scan
    prior_rows = pd.DataFrame()
    for back in range(1, 6):
        d = today - timedelta(days=back)
        prior_rows = _latest_per_ticker_on_date(scan_df, d)
        if not prior_rows.empty:
            break

    if prior_rows.empty:
        return []

    prior_map = {r["ticker"]: r for _, r in prior_rows.iterrows()}
    transitions = []
    for _, row in today_rows.iterrows():
        if row.get("status") != "READY":
            continue
        prev = prior_map.get(row["ticker"])
        if prev is not None and prev.get("status") == "WATCH":
            transitions.append(row.to_dict())
    return transitions


# ─────────────────────────────────────────────────────────────────────────────
# STALE SETUPS
# ─────────────────────────────────────────────────────────────────────────────
def _detect_stale_setups(scan_df: pd.DataFrame, today: date,
                          max_age_days: int) -> list[dict]:
    """
    Returns tickers that have been WATCH/READY for longer than max_age_days
    without ever being recorded as a trade.
    """
    if scan_df.empty:
        return []

    actionable = scan_df[scan_df["status"].isin(["READY", "WATCH"])].copy()
    if actionable.empty:
        return []

    first_seen = (actionable.groupby("ticker")["scan_date"]
                            .min().reset_index()
                            .rename(columns={"scan_date": "first_seen"}))
    last_seen  = (actionable.groupby("ticker")["scan_date"]
                            .max().reset_index()
                            .rename(columns={"scan_date": "last_seen"}))
    merged = first_seen.merge(last_seen, on="ticker")

    cutoff = today - timedelta(days=max_age_days)
    stale = []
    today_str = today.isoformat()
    for _, row in merged.iterrows():
        try:
            fs = datetime.strptime(str(row["first_seen"])[:10], "%Y-%m-%d").date()
            ls = datetime.strptime(str(row["last_seen"])[:10],  "%Y-%m-%d").date()
        except ValueError:
            continue
        age = (today - fs).days
        # Stale = first-seen too long ago AND still appearing today (last_seen == today)
        if fs <= cutoff and ls.isoformat() == today_str:
            stale.append({"ticker": row["ticker"], "first_seen": fs.isoformat(),
                          "age_days": age})
    return stale


# ─────────────────────────────────────────────────────────────────────────────
# GUARD VIOLATIONS  (would-be READY but blocked)
# ─────────────────────────────────────────────────────────────────────────────
def _detect_guard_blocks(today_rows: pd.DataFrame,
                          open_positions: list[dict]) -> list[dict]:
    """Runs guards over today's READY plans and returns those that fail."""
    blocks = []
    if today_rows.empty:
        return blocks

    ready = today_rows[today_rows["status"] == "READY"]
    for _, row in ready.iterrows():
        plan = {
            "ticker":       row["ticker"],
            "entry_price":  float(row.get("entry_price", 0) or 0),
            "max_loss_inr": float(row.get("max_loss_inr", 0) or 0),
        }
        ctx = {
            "current_price":  float(row.get("current_close", 0) or 0),
            "today_open":     0,   # not captured in scan_log; gap-up checked at --record
            "first_seen":     None,
            "open_positions": open_positions,
            "config":         CONFIG,
        }
        fails = run_all_guards(plan, ctx)
        if fails:
            blocks.append({
                "ticker":  row["ticker"],
                "reasons": [f.reason for f in fails],
                "codes":   [f.code for f in fails],
            })
    return blocks


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────────────────────────────────────
def run_alerts(journal_dir: str = "journal", quiet: bool = False) -> dict:
    """
    Runs the alert engine end-to-end.
    Returns a dict with the three lists. Side effects: console print,
    audit log append, Windows toast (best-effort).
    """
    today    = date.today()
    scan_df  = load_scan_log(journal_dir)
    trade_df = load_trade_log(journal_dir)

    # Build open_positions list compatible with guards.check_portfolio_heat
    open_t = trade_df[
        (trade_df["resolved"].astype(str) != "True") &
        (trade_df["exit_reason"].astype(str).isin(["OPEN", "", "nan"]))
    ]
    open_positions = []
    for _, r in open_t.iterrows():
        entry = float(r.get("entry_price", 0) or 0)
        stop  = float(r.get("stop_price",  0) or 0)
        open_positions.append({
            "ticker":          r.get("ticker", ""),
            "quantity":        float(r.get("quantity", 0) or 0),
            "risk_per_share":  max(0.0, entry - stop),
        })

    triggers = _detect_watch_to_ready(scan_df, today)
    stale    = _detect_stale_setups(scan_df, today,
                                     CONFIG.get("setup_expiry_days", 10))
    today_rows = _latest_per_ticker_on_date(scan_df, today)
    blocks   = _detect_guard_blocks(today_rows, open_positions)

    if not quiet:
        _render(triggers, stale, blocks)

    # Persist + toast
    for t in triggers:
        msg = f"{t['ticker']} just turned READY — entry ₹{float(t.get('entry_price', 0)):,.2f}"
        _log_alert(journal_dir, "TRIGGER", t["ticker"], msg)
        _toast("Trading Workstation — TRIGGER", msg)
    for s in stale:
        msg = f"{s['ticker']} stale ({s['age_days']}d) — take it or drop it"
        _log_alert(journal_dir, "STALE", s["ticker"], msg)
    for b in blocks:
        msg = f"{b['ticker']} BLOCKED — {'; '.join(b['reasons'])}"
        _log_alert(journal_dir, "BLOCKED", b["ticker"], msg)

    if not quiet and (triggers or blocks):
        _toast("Trading Workstation",
                f"{len(triggers)} new READY, {len(blocks)} blocked. See terminal.")

    return {"triggers": triggers, "stale": stale, "blocks": blocks}


# ─────────────────────────────────────────────────────────────────────────────
# RENDER
# ─────────────────────────────────────────────────────────────────────────────
def _render(triggers: list, stale: list, blocks: list) -> None:
    print("\n" + "═" * 70)
    print(f"  {_b('ALERTS')}  —  {date.today().isoformat()}")
    print("═" * 70)

    if not triggers and not stale and not blocks:
        print(f"  {_g('✓ No alerts. Watchlist quiet.')}\n")
        print("═" * 70 + "\n")
        return

    if triggers:
        print(f"\n  {_g('▲ TRIGGERED')}  WATCH → READY today:")
        for t in triggers:
            entry = float(t.get('entry_price', 0) or 0)
            score = t.get('score', 0)
            try:
                score_txt = f"score {float(score):.0f}" if pd.notna(score) else "score —"
            except (TypeError, ValueError):
                score_txt = "score —"
            print(f"    • {_c(t['ticker']):<20} entry ₹{entry:,.2f}   {score_txt}")

    if blocks:
        print(f"\n  {_y('✗ BLOCKED')}  READY but a guard rejected:")
        for b in blocks:
            print(f"    • {_c(b['ticker']):<20} {'; '.join(b['reasons'])}")

    if stale:
        print(f"\n  {_d('· STALE')}    setups older than configured limit:")
        for s in stale:
            print(f"    • {_c(s['ticker']):<20} first seen {s['first_seen']}  ({s['age_days']}d ago)")

    print("\n" + "═" * 70 + "\n")
