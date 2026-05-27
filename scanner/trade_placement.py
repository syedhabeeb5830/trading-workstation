"""
scanner/trade_placement.py — GTT order placement & lifecycle integration
==========================================================================
Commands wired into run.py:
  --place TICKER     create a GTT buy-stop from today's scan plan
  --gtts             list all active GTTs on the account

Also exposes:
  detect_and_record_fills(journal_dir) → polls Kite orders, auto-records
                                          any COMPLETE buys not already in
                                          the journal.

Design:
  • Discretionary — every placement asks for [y/N] confirmation.
  • Idempotent — won't auto-record the same fill twice.
  • Graceful — every Kite call wrapped; no crashes if session is stale.
"""

from __future__ import annotations
import csv
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from analytics.outcome_tracker import (
    record_trade_entry, load_trade_log
)
from analytics.scan_logger     import load_scan_log
from config.config             import CONFIG


# ── Colour helpers ───────────────────────────────────────────────────────────
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


GTT_LOG_COLUMNS = [
    "placed_at", "gtt_id", "ticker", "gtt_kind",       # BUY_STOP | PROTECTIVE
    "trigger_price", "limit_price", "quantity",
    "stop_price", "target_price",                     # only for PROTECTIVE
    "max_loss_inr", "status", "parent_trade_id",      # parent for PROTECTIVE
]


# ─────────────────────────────────────────────────────────────────────────────
# Helper: load today's plan for a ticker from the scan log
# ─────────────────────────────────────────────────────────────────────────────
def _todays_plan(ticker: str, journal_dir: str) -> Optional[dict]:
    scan_df = load_scan_log(journal_dir)
    if scan_df.empty or "scan_date" not in scan_df.columns:
        return None
    today_str = date.today().strftime("%Y-%m-%d")
    match = scan_df[
        (scan_df["scan_date"] == today_str) &
        (scan_df["ticker"]    == ticker)
    ].tail(1)
    if match.empty:
        return None
    row = match.iloc[0]
    return {k: row.get(k, 0) for k in [
        "ticker", "grade", "score", "tier", "status",
        "entry_price", "stop_price", "t1", "t2",
        "rr_t1", "quantity", "max_loss_inr",
    ]} | {"ticker": ticker, "regime": str(row.get("regime", "BULL"))}


# ─────────────────────────────────────────────────────────────────────────────
# COMMAND: --place TICKER
# ─────────────────────────────────────────────────────────────────────────────
def place_gtt_interactive(ticker: str, journal_dir: str = "journal") -> None:
    """
    Interactive GTT placement:
      1. Looks up today's plan in scan log
      2. Shows the plan + max loss in ₹
      3. Asks for [y/N] confirmation
      4. Places GTT on Zerodha, logs the gtt_id to journal/gtt_orders.csv
    """
    print(f"\n  {_b('PLACE GTT')}  —  {ticker}\n")

    plan = _todays_plan(ticker, journal_dir)
    if plan is None or float(plan.get("entry_price", 0) or 0) <= 0:
        print(_r(f"  ✗ {ticker} not in today's scan log."))
        print(_d("    Run --today first, or use --record for an off-plan trade.\n"))
        return

    if str(plan.get("status", "")) not in ("READY", "WATCH"):
        print(_y(f"  ⚠  {ticker} status is '{plan['status']}' — not READY/WATCH."))
        print(_d("    Continuing anyway (you may be placing a forward alert).\n"))

    # ── REGIME GATE ────────────────────────────────────────────────────────
    # Fetch current NIFTY regime and apply the configured gate policy.
    try:
        from scanner.scanner import get_market_regime
        from scanner.guards import check_regime_gate
        from config.config import CONFIG as _CFG
        _regime_data = get_market_regime(_CFG)
        _regime      = _regime_data["regime"]
        _gate        = check_regime_gate(_regime, _CFG)

        _regime_color = {"BULL": _g, "NEUTRAL": _y, "BEAR": _r}.get(_regime, _d)
        print(f"  Regime      {_regime_color(_regime):<20}  "
              f"(strength {_regime_data['strength']:.2f})\n")

        if not _gate.allowed:
            # BEAR — hard block
            print(_r(f"  ✗ REGIME GATE: {_gate.reason}"))
            print(_r( "    ─────────────────────────────────────────────────"))
            print(_r( "    Opening new longs in a BEAR market amplifies losses."))
            print(_r( "    The index will drag down even strong individual setups."))
            print()
            _override = input(
                _y("  Override and place anyway? type 'override' to confirm, or press Enter to cancel: ")
            ).strip().lower()
            if _override != "override":
                print(_d("\n  Smart call. No order placed.\n"))
                return
            print(_y("\n  ⚠  Override accepted. Proceeding against regime.\n"))

        elif _gate.code == "WARN":
            # NEUTRAL — warn but allow
            print(_y(f"  ⚠  REGIME GATE: {_gate.reason}"))
            print(_y( "    Consider reducing position size or skipping marginal setups."))
            print()
            _cont = input(_b("  Proceed? [y/N]: ")).strip().lower()
            if _cont != "y":
                print(_d("\n  Cancelled.\n"))
                return
            print()

    except Exception:
        pass  # regime check is advisory — never block --place due to a code error

    qty = int(float(plan.get("quantity", 0) or 0))
    if qty <= 0:
        print(_r(f"  ✗ Plan has quantity 0 — increase capital or change tier.\n"))
        return

    entry  = float(plan["entry_price"])
    stop   = float(plan["stop_price"])
    t1     = float(plan["t1"])
    t2     = float(plan["t2"])
    mloss  = float(plan.get("max_loss_inr", (entry - stop) * qty))

    print(f"  Tier        {plan.get('tier','-'):<10}  Status  {plan.get('status','-')}")
    print(f"  Entry       ₹{entry:>10,.2f}   (buy-stop trigger)")
    print(f"  Stop        ₹{stop:>10,.2f}   ({((stop-entry)/entry*100):+.1f}%)")
    print(f"  T1 / T2     ₹{t1:,.2f}  /  ₹{t2:,.2f}")
    print(f"  Quantity    {qty:,} shares")
    print(f"  Max loss    {_r(f'₹{mloss:,.0f}')}    "
          f"{_d(f'(if stop is hit, you lose this much)')}\n")

    # Pre-checks via Kite
    try:
        from integrations.zerodha import get_client_or_none
        kc = get_client_or_none()
    except ImportError:
        kc = None
    if kc is None:
        print(_r("  ✗ Kite session is not active. Run: python run.py --kite-login\n"))
        return

    # Show live capital + available cash check
    try:
        m = kc.margins_equity()
        capital_required = entry * qty
        avail = m["available_cash"]
        print(f"  {_b('Live capital')}    ₹{m['live_balance']:>10,.0f}   "
              f"Available ₹{avail:>10,.0f}")
        print(f"  {_b('This trade')}      ₹{capital_required:>10,.0f}   "
              f"({capital_required/m['live_balance']*100:.1f}% of capital)\n")
        if capital_required > avail:
            print(_r(f"  ✗ Available cash (₹{avail:,.0f}) < required (₹{capital_required:,.0f})"))
            print(_d("    Aborting. Deposit funds and retry.\n"))
            return
    except Exception:
        print(_d("  (Could not verify margins — proceeding anyway)\n"))

    confirm = input(_b(f"  Place GTT buy-stop on Zerodha for {ticker}? [y/N]: ")).strip().lower()
    if confirm != "y":
        print(_d("\n  Cancelled. No order placed.\n"))
        return

    # Place the GTT
    try:
        gtt_id = kc.place_gtt_buy_stop(
            ticker=ticker,
            trigger_price=entry,
            quantity=qty,
            limit_price=round(entry * 1.005, 1),  # 0.5% buffer
        )
    except Exception as e:
        print(_r(f"\n  ✗ Kite rejected the order: {e}\n"))
        return

    # Persist to journal/gtt_orders.csv
    _log_gtt(journal_dir, gtt_id, ticker, entry, round(entry * 1.005, 1),
              qty, mloss, status="ACTIVE")

    print(_g(f"\n  ✓ GTT placed successfully.  trigger_id = {gtt_id}"))
    print(f"     Trigger:  ₹{entry:,.2f}")
    print(f"     Quantity: {qty:,} shares (CNC)")
    print(_d(f"     Cancel later with: python run.py --gtts (then delete from Kite web)"))
    print(_d(f"     On fill, run --positions to auto-record this trade.\n"))


# ─────────────────────────────────────────────────────────────────────────────
# COMMAND: --gtts
# ─────────────────────────────────────────────────────────────────────────────
def list_gtts(journal_dir: str = "journal") -> None:
    """Print all GTTs currently on the account."""
    print(f"\n  {_b('GTT ORDERS')}  (Zerodha server-side triggers)\n")
    try:
        from integrations.zerodha import get_client_or_none
        kc = get_client_or_none()
    except ImportError:
        kc = None
    if kc is None:
        print(_r("  ✗ Kite session not active. Run: python run.py --kite-login\n"))
        return

    gtts = kc.list_gtts()
    if not gtts:
        print(_d("  No GTTs on account.\n"))
        return

    print(f"  {'GTT_ID':<12} {'TICKER':<14} {'TYPE':<6} {'TRIGGER':>10} "
          f"{'QTY':>6}  {'STATUS':<10}  CREATED")
    print(_d("  " + "─" * 78))
    for g in gtts:
        gid     = g.get("id", "-")
        cond    = g.get("condition", {}) or {}
        tsym    = cond.get("tradingsymbol", "")
        exch    = cond.get("exchange", "NSE")
        trig    = (cond.get("trigger_values") or [0])[0]
        orders  = g.get("orders") or [{}]
        qty     = orders[0].get("quantity", 0)
        ttype   = orders[0].get("transaction_type", "?")
        status  = g.get("status", "?")
        created = str(g.get("created_at", ""))[:16]
        sclr = _g if status == "active" else (_y if status == "triggered" else _d)
        tickerd = f"{tsym}.NS" if exch == "NSE" else f"{tsym}.BO"
        print(f"  {str(gid):<12} {tickerd:<14} {ttype:<6} {trig:>10.2f} "
              f"{int(qty):>6}  {sclr(status):<10}  {created}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-FILL DETECTION  — called from --positions to catch unrecorded fills
# ─────────────────────────────────────────────────────────────────────────────
def detect_and_record_fills(journal_dir: str = "journal") -> int:
    """
    Polls today's Kite orders for COMPLETE buys; auto-records any that
    don't already exist in the journal. Returns the number recorded.

    Silent (no print) when zero fills. Logs each auto-record.
    """
    try:
        from integrations.zerodha import get_client_or_none
        kc = get_client_or_none()
    except ImportError:
        return 0
    if kc is None:
        return 0

    try:
        all_orders = kc.orders() or []
    except Exception:
        return 0

    today_str = date.today().strftime("%Y-%m-%d")

    # Build set of already-recorded tickers for today
    try:
        journal = load_trade_log(journal_dir)
        if not journal.empty:
            existing = set(
                str(r["ticker"])
                for _, r in journal.iterrows()
                if str(r.get("entry_date", ""))[:10] == today_str
            )
        else:
            existing = set()
    except Exception:
        existing = set()

    recorded = 0
    for o in all_orders:
        if o.get("status") != "COMPLETE":             continue
        if o.get("transaction_type") != "BUY":        continue
        ts_raw = str(o.get("order_timestamp", ""))[:10]
        if ts_raw and ts_raw != today_str:            continue
        tsym = o.get("tradingsymbol", "")
        exch = o.get("exchange", "NSE")
        if not tsym:                                  continue
        ticker = f"{tsym}.NS" if exch == "NSE" else f"{tsym}.BO"
        if ticker in existing:                        continue

        # Build a minimal plan from the actual fill + today's scan log
        plan = _todays_plan(ticker, journal_dir) or {}
        fill_price = float(o.get("average_price", 0) or 0)
        qty        = int(float(o.get("filled_quantity", 0) or 0))
        if fill_price <= 0 or qty <= 0:               continue

        # If scan log had a plan, use its stop/T1/T2; else stub
        entry = plan.get("entry_price", fill_price)
        stop  = plan.get("stop_price",  round(fill_price * 0.97, 2))
        t1    = plan.get("t1",          round(fill_price * 1.06, 2))
        t2    = plan.get("t2",          round(fill_price * 1.09, 2))
        risk  = max(0.01, float(entry) - float(stop))
        plan_to_log = {
            "ticker":            ticker,
            "grade":             plan.get("grade", "AUTO"),
            "score":             plan.get("score", ""),
            "entry_price":       fill_price,     # use actual fill, not plan trigger
            "stop_price":        stop,
            "t1":                t1,
            "t2":                t2,
            "rr_t1":             round((t1 - fill_price) / risk, 2),
            "quantity":          qty,
            "max_loss_inr":      round(qty * risk, 2),
            "atr_pct":           plan.get("atr_pct", 0),
            "relative_strength": plan.get("relative_strength", 0),
            "range_pct":         plan.get("range_pct", 0),
        }
        regime = plan.get("regime", "BULL")
        try:
            tid = record_trade_entry(
                plan_to_log, regime,
                entry_date=today_str,
                notes=f"AUTO-RECORDED from Kite fill (order {o.get('order_id','?')})",
                journal_dir=journal_dir,
            )
            print(_g(f"  ✓ Auto-recorded fill: {ticker} @ ₹{fill_price:,.2f}  "
                     f"qty {qty:,}  (trade_id {tid})"))
            recorded += 1
            existing.add(ticker)

            # ── PROTECTIVE GTT: bracket the fresh position with stop+target ──
            # This is the most important step: prevents an overnight gap from
            # turning a 1R loss into a 3R loss.
            try:
                gtt_id = kc.place_gtt_protective(
                    ticker=ticker,
                    stop_price=float(stop),
                    target_price=float(t2),
                    quantity=qty,
                    last_price=fill_price,
                )
                _log_gtt(
                    journal_dir, gtt_id, ticker,
                    trigger=float(stop), limit=float(stop) * 0.995,
                    qty=qty, max_loss=plan_to_log["max_loss_inr"],
                    status="ACTIVE", gtt_kind="PROTECTIVE",
                    stop_price=float(stop), target_price=float(t2),
                    parent_trade_id=tid,
                )
                print(_g(f"    ✓ Protective GTT placed: stop ₹{stop:,.2f}  "
                         f"target ₹{t2:,.2f}  (gtt_id {gtt_id})"))
                gtt_ok = True
            except Exception as e:
                print(_r(f"    ✗ PROTECTIVE GTT FAILED for {ticker}: {e}"))
                print(_r(f"      → MANUAL STOP REQUIRED. Place SL @ ₹{stop:,.2f} now."))
                gtt_ok = False

            # ── Notifications: desktop toast + Telegram ──
            risk_inr   = float(plan_to_log["max_loss_inr"])
            try:
                from config.config import CONFIG
                cap = float(CONFIG.get("account_capital", 100_000))
                heat_pct = round(risk_inr / cap * 100, 2) if cap else 0.0
            except Exception:
                heat_pct = 0.0
            _notify_fill(ticker, fill_price, qty, stop)
            try:
                from integrations.telegram_notifier import notify_fill as tg_fill
                tg_fill(
                    ticker=ticker, fill_price=fill_price, qty=qty,
                    stop=float(stop), target1=float(t1),
                    risk_inr=risk_inr, portfolio_heat_pct=heat_pct,
                    gtt_placed=gtt_ok,
                )
            except Exception:
                pass

        except Exception as e:
            print(_y(f"  ⚠ Could not auto-record {ticker}: {e}"))

    return recorded


def _notify_fill(ticker: str, fill_price: float, qty: int, stop: float) -> None:
    """Best-effort Windows toast. Silent if winotify unavailable."""
    try:
        from winotify import Notification, audio
        n = Notification(
            app_id="Swing Trader",
            title=f"FILLED: {ticker}",
            msg=f"Bought {qty:,} @ ₹{fill_price:,.2f}  •  SL @ ₹{stop:,.2f}",
            duration="short",
        )
        n.set_audio(audio.Default, loop=False)
        n.show()
    except Exception:
        pass


def _notify_alert(title: str, msg: str, telegram_msg: str = "") -> None:
    """Generic toast + optional Telegram message for GTT/stop/target events."""
    try:
        from winotify import Notification, audio
        n = Notification(app_id="Swing Trader", title=title,
                          msg=msg, duration="short")
        n.set_audio(audio.Default, loop=False)
        n.show()
    except Exception:
        pass
    if telegram_msg:
        try:
            from integrations.telegram_notifier import notify_raw
            notify_raw(telegram_msg)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Internal: append a row to journal/gtt_orders.csv
# ─────────────────────────────────────────────────────────────────────────────
def _log_gtt(journal_dir: str, gtt_id: int, ticker: str,
             trigger: float, limit: float, qty: int,
             max_loss: float, status: str,
             gtt_kind: str = "BUY_STOP",
             stop_price: float = 0.0, target_price: float = 0.0,
             parent_trade_id: str = "") -> None:
    p = Path(journal_dir) / "gtt_orders.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    new_file = not p.exists() or p.stat().st_size == 0
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=GTT_LOG_COLUMNS, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerow({
            "placed_at":       datetime.now().isoformat(timespec="seconds"),
            "gtt_id":          gtt_id,
            "ticker":          ticker,
            "gtt_kind":        gtt_kind,
            "trigger_price":   trigger,
            "limit_price":     limit,
            "quantity":        qty,
            "stop_price":      stop_price,
            "target_price":    target_price,
            "max_loss_inr":    max_loss,
            "status":          status,
            "parent_trade_id": parent_trade_id,
        })
