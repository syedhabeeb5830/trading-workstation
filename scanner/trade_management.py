"""
scanner/trade_management.py — In-trade actions
================================================
Two commands wired into run.py for active position management:

  --trail TICKER PRICE      Raise stop on an open trade (updates journal +
                            replaces the protective GTT on Zerodha).

  --partial TICKER QTY      Record a partial exit at current/specified price
                            (books R on the partial, updates journal + reduces
                            the protective GTT's remaining quantity).

Design:
  • Both edit the open trades.csv row in place (not append).
  • Both attempt to mirror the action on Zerodha via the protective GTT.
  • Both fall back gracefully if Kite is unavailable — journal is still
    updated, and a manual instruction is printed.
"""
from __future__ import annotations
import csv
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

from analytics.outcome_tracker import (
    TRADE_COLUMNS, get_trade_log_path, load_trade_log,
)


# ── colours ─────────────────────────────────────────────────────────────────
_G, _Y, _R, _C, _D, _B, _RST = (
    "\033[92m", "\033[93m", "\033[91m",
    "\033[96m", "\033[2m",  "\033[1m", "\033[0m",
)
def _g(s): return f"{_G}{s}{_RST}"
def _y(s): return f"{_Y}{s}{_RST}"
def _r(s): return f"{_R}{s}{_RST}"
def _c(s): return f"{_C}{s}{_RST}"
def _d(s): return f"{_D}{s}{_RST}"
def _b(s): return f"{_B}{s}{_RST}"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _find_open_trade(ticker: str, journal_dir: str) -> Optional[pd.Series]:
    """Returns the (single) open trade row for ticker, or None."""
    df = load_trade_log(journal_dir)
    if df.empty:
        return None
    open_rows = df[
        (df["ticker"] == ticker) &
        (df["exit_reason"].astype(str).isin(["OPEN", "", "nan"]))
    ]
    if open_rows.empty:
        return None
    # Latest open trade (in case of duplicates — defensive)
    return open_rows.iloc[-1]


def _rewrite_trade_row(journal_dir: str, trade_id: str,
                        updates: dict) -> bool:
    """Updates a single row in trades.csv by trade_id. Returns success."""
    path = get_trade_log_path(journal_dir)
    if not path.exists():
        return False
    df = pd.read_csv(path)
    if "trade_id" not in df.columns or trade_id not in df["trade_id"].values:
        return False

    # Ensure all current schema columns exist (back-fill if migrating)
    for col in TRADE_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    mask = df["trade_id"] == trade_id
    for k, v in updates.items():
        df.loc[mask, k] = v
    df.to_csv(path, index=False, columns=TRADE_COLUMNS)
    return True


def _find_protective_gtt(ticker: str, journal_dir: str) -> Optional[dict]:
    """
    Returns the most recent ACTIVE protective GTT row for `ticker` from
    journal/gtt_orders.csv, or None.
    """
    p = Path(journal_dir) / "gtt_orders.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p)
    if df.empty:
        return None
    df = df[
        (df["ticker"] == ticker) &
        (df.get("gtt_kind", "BUY_STOP") == "PROTECTIVE") &
        (df.get("status", "") == "ACTIVE")
    ]
    if df.empty:
        return None
    return df.iloc[-1].to_dict()


def _mark_gtt_status(journal_dir: str, gtt_id, new_status: str) -> None:
    p = Path(journal_dir) / "gtt_orders.csv"
    if not p.exists():
        return
    df = pd.read_csv(p)
    df.loc[df["gtt_id"].astype(str) == str(gtt_id), "status"] = new_status
    df.to_csv(p, index=False)


def _live_price(ticker: str) -> Optional[float]:
    try:
        from scanner.live_data import enrich_with_live
        snap = enrich_with_live([ticker]).get(ticker, {})
        ltp = float(snap.get("ltp", 0) or 0)
        return ltp if ltp > 0 else None
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════════
# COMMAND: --trail TICKER NEW_STOP
# ═════════════════════════════════════════════════════════════════════════════
def trail_stop(ticker: str, new_stop: float,
                journal_dir: str = "journal") -> None:
    print(f"\n  {_b('TRAIL STOP')}  —  {ticker}\n")

    trade = _find_open_trade(ticker, journal_dir)
    if trade is None:
        print(_r(f"  ✗ No open trade for {ticker} in journal.\n"))
        return

    entry   = float(trade["entry_price"])
    cur_stop = float(trade.get("current_stop") or trade["stop_price"])
    qty     = int(float(trade["quantity"]))
    t2      = float(trade.get("t2", entry * 1.09))
    tid     = str(trade["trade_id"])

    if new_stop <= cur_stop:
        print(_y(f"  ⚠  New stop ₹{new_stop:,.2f} is not higher than current ₹{cur_stop:,.2f}."))
        print(_d("    Refusing to lower a stop. Aborting.\n"))
        return
    if new_stop >= entry * 1.10:
        print(_y(f"  ⚠  New stop ₹{new_stop:,.2f} is >10% above entry — sanity check failed."))
        return

    risk_per_share = max(0.01, entry - float(trade["stop_price"]))
    locked_r = (new_stop - entry) / risk_per_share

    print(f"  Entry           ₹{entry:>10,.2f}")
    print(f"  Old stop        ₹{cur_stop:>10,.2f}")
    print(f"  {_g('New stop')}        ₹{new_stop:>10,.2f}   "
          f"({_g(f'locks in {locked_r:+.2f}R') if locked_r >= 0 else _r(f'still risking {-locked_r:.2f}R')})")
    print(f"  Quantity        {qty:>10,}\n")

    confirm = input(_b("  Confirm trail? [y/N]: ")).strip().lower()
    if confirm != "y":
        print(_d("\n  Cancelled.\n"))
        return

    # 1. Update journal
    _rewrite_trade_row(journal_dir, tid, {"current_stop": new_stop})
    print(_g(f"  ✓ Journal updated: stop now ₹{new_stop:,.2f}"))

    # 2. Replace protective GTT
    gtt = _find_protective_gtt(ticker, journal_dir)
    if gtt is None:
        print(_y("  ⚠  No protective GTT on file for this trade."))
        print(_d(f"    → Manually update SL to ₹{new_stop:,.2f} on Kite, "
                 f"or wait for --positions to auto-place one.\n"))
        return

    try:
        from integrations.zerodha import get_client_or_none
        kc = get_client_or_none()
    except ImportError:
        kc = None
    if kc is None:
        print(_y("  ⚠  Kite session not active — broker-side GTT unchanged."))
        print(_d(f"    → Run --kite-login then --trail again to sync.\n"))
        return

    try:
        old_id = int(gtt["gtt_id"])
        new_id = kc.modify_gtt_protective(
            trigger_id=old_id, ticker=ticker,
            stop_price=new_stop, target_price=t2, quantity=qty,
        )
        _mark_gtt_status(journal_dir, old_id, "REPLACED")
        from scanner.trade_placement import _log_gtt
        _log_gtt(journal_dir, new_id, ticker,
                  trigger=new_stop, limit=new_stop * 0.995, qty=qty,
                  max_loss=qty * max(0.01, entry - new_stop),
                  status="ACTIVE", gtt_kind="PROTECTIVE",
                  stop_price=new_stop, target_price=t2,
                  parent_trade_id=tid)
        print(_g(f"  ✓ Protective GTT replaced  (old {old_id} → new {new_id})\n"))        try:
            from integrations.telegram_notifier import notify_stop_trailed
            notify_stop_trailed(
                ticker=ticker, old_stop=cur_stop,
                new_stop=new_stop, locked_r=locked_r,
            )
        except Exception:
            pass    except Exception as e:
        print(_r(f"  ✗ Failed to replace GTT: {e}"))
        print(_y(f"    → MANUALLY update SL trigger to ₹{new_stop:,.2f} on Kite NOW.\n"))


# ═════════════════════════════════════════════════════════════════════════════
# COMMAND: --partial TICKER QTY  [--price PRICE]
# ═════════════════════════════════════════════════════════════════════════════
def partial_exit(ticker: str, exit_qty: int,
                  price: Optional[float] = None,
                  journal_dir: str = "journal") -> None:
    print(f"\n  {_b('PARTIAL EXIT')}  —  {ticker}\n")

    trade = _find_open_trade(ticker, journal_dir)
    if trade is None:
        print(_r(f"  ✗ No open trade for {ticker} in journal.\n"))
        return

    qty       = int(float(trade["quantity"]))
    entry     = float(trade["entry_price"])
    stop_orig = float(trade["stop_price"])
    t2        = float(trade.get("t2", entry * 1.09))
    tid       = str(trade["trade_id"])

    if str(trade.get("partial_qty", "") or "") not in ("", "0", "0.0", "nan"):
        print(_y("  ⚠  This trade already has a partial exit recorded."))
        print(_d("    Multi-stage partials aren't supported yet — close the trade fully with --resolve.\n"))
        return
    if exit_qty <= 0 or exit_qty >= qty:
        print(_r(f"  ✗ Partial qty must be between 1 and {qty - 1} (full size).\n"))
        return

    if price is None:
        live = _live_price(ticker)
        if live is None:
            print(_r("  ✗ No live price available. Pass --price explicitly.\n"))
            return
        price = live
        print(_d(f"  Using live LTP: ₹{price:,.2f}"))

    risk_per_share = max(0.01, entry - stop_orig)
    partial_r = (price - entry) / risk_per_share
    booked_inr = (price - entry) * exit_qty
    remaining = qty - exit_qty

    print(f"  Entry           ₹{entry:>10,.2f}   Stop ₹{stop_orig:,.2f}")
    print(f"  Sell qty        {exit_qty:>10,}  of {qty:,}   (remaining {remaining:,})")
    print(f"  Sell price      ₹{price:>10,.2f}")
    rclr = _g if partial_r >= 0 else _r
    print(f"  Booked          {rclr(f'{partial_r:+.2f}R')}   ({rclr(f'₹{booked_inr:+,.0f}')})\n")

    # Standard playbook: move stop to breakeven after first partial
    suggested_new_stop = entry
    print(_d(f"  → After this partial, suggested stop = breakeven (₹{suggested_new_stop:,.2f})\n"))

    confirm = input(_b("  Confirm partial? [y/N]: ")).strip().lower()
    if confirm != "y":
        print(_d("\n  Cancelled.\n"))
        return

    # 1. Update journal: record partial fields
    today_str = date.today().strftime("%Y-%m-%d")
    _rewrite_trade_row(journal_dir, tid, {
        "partial_qty":   exit_qty,
        "partial_price": round(price, 2),
        "partial_date":  today_str,
        "partial_r":     round(partial_r, 3),
        "current_stop":  suggested_new_stop,  # auto-bump to breakeven
    })
    print(_g(f"  ✓ Journal: booked {partial_r:+.2f}R on {exit_qty} shares, "
             f"stop moved to breakeven."))

    # 2. Modify protective GTT to reduce qty + raise stop
    gtt = _find_protective_gtt(ticker, journal_dir)
    if gtt is None:
        print(_y("  ⚠  No protective GTT on file. Manually adjust SL on Kite."))
        print()
        return

    try:
        from integrations.zerodha import get_client_or_none
        kc = get_client_or_none()
    except ImportError:
        kc = None
    if kc is None:
        print(_y("  ⚠  Kite session not active — broker-side GTT unchanged."))
        print(_d(f"    → Manually: (a) market-sell {exit_qty} shares; "
                 f"(b) modify SL to ₹{suggested_new_stop:,.2f} for {remaining} shares.\n"))
        return

    try:
        old_id = int(gtt["gtt_id"])
        new_id = kc.modify_gtt_protective(
            trigger_id=old_id, ticker=ticker,
            stop_price=suggested_new_stop, target_price=t2,
            quantity=remaining,
        )
        _mark_gtt_status(journal_dir, old_id, "REPLACED")
        from scanner.trade_placement import _log_gtt
        _log_gtt(journal_dir, new_id, ticker,
                  trigger=suggested_new_stop, limit=suggested_new_stop * 0.995,
                  qty=remaining,
                  max_loss=remaining * max(0.01, entry - suggested_new_stop),
                  status="ACTIVE", gtt_kind="PROTECTIVE",
                  stop_price=suggested_new_stop, target_price=t2,
                  parent_trade_id=tid)
        print(_g(f"  ✓ Protective GTT replaced  ({remaining} shares, stop = entry)\n"))
        print(_y(f"  ⚠  REMINDER: This only updates the SL bracket. You still need to"))
        print(_y(f"     market-sell {exit_qty} shares on Kite to actually book the partial."))
        print(_d(f"     (Or place a limit-sell for {exit_qty} @ market.)\n"))        try:
            from integrations.telegram_notifier import notify_partial_exit
            notify_partial_exit(
                ticker=ticker, qty=exit_qty, price=round(price, 2),
                partial_r=round(partial_r, 3), new_stop=suggested_new_stop,
            )
        except Exception:
            pass    except Exception as e:
        print(_r(f"  ✗ Failed to replace GTT: {e}"))
        print(_y(f"    → Manually adjust SL on Kite.\n"))
