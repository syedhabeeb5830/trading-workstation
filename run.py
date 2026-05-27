"""
run.py — Swing Trading Workstation
====================================
Three commands cover 95% of daily use:

  MORNING (pre-market):
    python run.py --today                 ← cockpit: scan + alerts + capital
    python run.py --place TICKER          ← place GTT buy-stop on Zerodha

  INTRADAY (any time, or scheduled):
    python run.py --positions             ← live R, auto-records new fills,
                                            auto-places protective SL
    python run.py --trail TICKER STOP     ← raise stop on an open trade
    python run.py --partial TICKER QTY    ← book partial at T1, move to breakeven

  EOD (after market close):
    python run.py --reconcile             ← journal ↔ broker truth check
    python run.py --resolve               ← update closed trade outcomes

  WEEKLY review:
    python run.py --report [TYPE]         ← analytics; TYPE=full|expectancy|
                                            segment|edge|equity|scanlog

  ONE-TIME SETUP:
    python run.py --install-scheduler     ← auto-run --positions every 15min
    python run.py --kite-login            ← refresh Kite session (also auto)

  ADVANCED / occasional:
    python run.py --gtts                  ← list all GTTs on the account
    python run.py --alerts                ← run alert engine standalone
    python run.py --debug                 ← why setups failed (strategy review)
    python run.py --record TICKER         ← manually log an off-plan trade
    python run.py --sync                  ← pull from Kite (auto in reconcile)

Tip: add a shell alias
  alias today='cd ~/TRADING_WORKSTATION && python run.py --today'
"""

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Swing Trade Workstation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
        add_help=True,
    )

    # ── Core flags ────────────────────────────────────────────────────────────
    parser.add_argument(
        "--today",
        action="store_true",
        help="Daily execution cockpit (primary workflow)",
    )
    parser.add_argument(
        "--positions",
        action="store_true",
        help="Active position cockpit — live R, auto-records new fills",
    )
    parser.add_argument(
        "--place",
        metavar="TICKER",
        help="Place a GTT buy-stop on Zerodha from today's plan",
    )
    parser.add_argument(
        "--gtts",
        action="store_true",
        help="List all GTT orders currently on your Zerodha account",
    )
    parser.add_argument(
        "--record",
        metavar="TICKER",
        help="Log a trade taken today (e.g. --record DIXON.NS)",
    )
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="Auto-resolve open trade outcomes via yfinance",
    )
    parser.add_argument(
        "--report",
        nargs="?",
        const="full",
        metavar="TYPE",
        help="Analytics report: full | expectancy | segment | edge | equity | scanlog",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Full pipeline audit — all tickers, all rejections, score breakdowns",
    )
    parser.add_argument(
        "--alerts",
        action="store_true",
        help="Run alert engine: WATCH→READY, stale setups, guard blocks",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Pull live holdings + positions from Zerodha Kite",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="Diff journal vs broker (auto-syncs first)",
    )
    parser.add_argument(
        "--kite-login",
        action="store_true",
        dest="kite_login",
        help="One-time-per-day OAuth flow for Kite Connect (auto-called)",
    )
    parser.add_argument(
        "--trail",
        nargs=2, metavar=("TICKER", "NEW_STOP"),
        help="Raise the stop on an open trade (also modifies the protective GTT)",
    )
    parser.add_argument(
        "--partial",
        nargs=2, metavar=("TICKER", "QTY"),
        help="Book a partial exit on an open trade (auto-moves stop to breakeven)",
    )
    parser.add_argument(
        "--install-scheduler",
        action="store_true", dest="install_scheduler",
        help="Register Windows Task to run --positions every 15min during market hours",
    )
    parser.add_argument(
        "--uninstall-scheduler",
        action="store_true", dest="uninstall_scheduler",
        help="Remove the scheduled --positions task",
    )
    parser.add_argument(
        "--weekly-pulse",
        action="store_true", dest="weekly_pulse",
        help="Send weekly performance summary to Telegram (any day)",
    )

    # ── Modifiers ─────────────────────────────────────────────────────────────
    parser.add_argument(
        "--notes",
        metavar="TEXT",
        default="",
        help="Trade notes (used with --record)",
    )
    parser.add_argument(
        "--price",
        metavar="PRICE",
        type=float,
        default=None,
        help="Override price for --partial (default: live LTP)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-resolve all trades, not just open ones (used with --resolve)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Minimal output (for scheduled runs)",
    )
    parser.add_argument(
        "--journal",
        metavar="DIR",
        default="journal",
        help="Journal directory (default: journal/)",
    )

    args = parser.parse_args()

    # ─────────────────────────────────────────────────────────────────────────
    # DISPATCH
    # ─────────────────────────────────────────────────────────────────────────

    if args.today:
        _cmd_today(args.journal)

    elif args.positions:
        _cmd_positions(args.journal, quiet=args.quiet)

    elif args.place:
        _cmd_place(args.place, args.journal)

    elif args.gtts:
        _cmd_gtts(args.journal)

    elif args.trail:
        _cmd_trail(args.trail[0], args.trail[1], args.journal)

    elif args.partial:
        _cmd_partial(args.partial[0], args.partial[1], args.price, args.journal)

    elif args.install_scheduler:
        _cmd_install_scheduler()

    elif args.uninstall_scheduler:
        _cmd_uninstall_scheduler()

    elif args.weekly_pulse:
        _cmd_weekly_pulse(args.journal, force=True)

    elif args.alerts:
        _cmd_alerts(args.journal)

    elif args.kite_login:
        _cmd_kite_login()

    elif args.sync:
        _cmd_sync(args.journal)

    elif args.reconcile:
        _cmd_reconcile(args.journal)

    elif args.debug:
        _cmd_debug(args.journal)

    elif args.record:
        _cmd_record(args.record, args.notes, args.journal)

    elif args.resolve:
        _cmd_resolve(args.journal, args.force)

    elif args.report is not None:
        _cmd_report(args.report, args.journal)

    else:
        # No flag given — default to --today
        _cmd_today(args.journal)


# ─────────────────────────────────────────────────────────────────────────────
# COMMAND IMPLEMENTATIONS
# ─────────────────────────────────────────────────────────────────────────────

def _cmd_today(journal_dir: str) -> None:
    """Daily execution cockpit — primary workflow."""
    from scanner.daily_mode import run_daily_mode
    run_daily_mode(journal_dir=journal_dir)

def _cmd_positions(journal_dir: str, quiet: bool = False) -> None:
    """Active position cockpit — manage open trades."""
    from scanner.positions import run_positions_mode
    run_positions_mode(journal_dir=journal_dir, quiet=quiet)


def _cmd_place(ticker: str, journal_dir: str) -> None:
    """Interactive GTT buy-stop placement."""
    from scanner.trade_placement import place_gtt_interactive
    place_gtt_interactive(ticker.upper(), journal_dir=journal_dir)


def _cmd_gtts(journal_dir: str) -> None:
    """List all GTT orders on the Zerodha account."""
    from scanner.trade_placement import list_gtts
    list_gtts(journal_dir=journal_dir)


def _cmd_trail(ticker: str, new_stop: str, journal_dir: str) -> None:
    """Raise stop on an open trade."""
    from scanner.trade_management import trail_stop
    try:
        ns = float(new_stop)
    except ValueError:
        print(f"\n  ✗ Invalid stop price: {new_stop}\n"); return
    trail_stop(ticker.upper(), ns, journal_dir=journal_dir)


def _cmd_partial(ticker: str, qty: str, price, journal_dir: str) -> None:
    """Book a partial exit at current/specified price."""
    from scanner.trade_management import partial_exit
    try:
        q = int(qty)
    except ValueError:
        print(f"\n  ✗ Invalid quantity: {qty}\n"); return
    partial_exit(ticker.upper(), q, price=price, journal_dir=journal_dir)


def _cmd_install_scheduler() -> None:
    from scanner.scheduler import install_scheduler
    install_scheduler()


def _cmd_uninstall_scheduler() -> None:
    from scanner.scheduler import uninstall_scheduler
    uninstall_scheduler()


def _cmd_alerts(journal_dir: str) -> None:
    """Alert engine — WATCH→READY, stale setups, guard blocks."""
    from scanner.alerts import run_alerts
    run_alerts(journal_dir=journal_dir)


def _cmd_kite_login() -> None:
    """One-time-per-day Kite Connect OAuth flow."""
    from integrations.zerodha import login_interactive
    login_interactive()


def _cmd_sync(journal_dir: str) -> None:
    """Pull live holdings + positions from Zerodha."""
    from scanner.reconcile import run_sync
    run_sync(journal_dir=journal_dir)


def _cmd_reconcile(journal_dir: str) -> None:
    """Diff journal open trades against Zerodha snapshot."""
    from scanner.reconcile import run_reconcile
    run_reconcile(journal_dir=journal_dir, auto_sync=True)


def _cmd_debug(journal_dir: str) -> None:
    """Full pipeline audit — for strategy refinement, NOT daily trading."""
    from scanner.debug_mode import run_debug_mode
    run_debug_mode(journal_dir=journal_dir)


def _cmd_record(ticker: str, notes: str, journal_dir: str) -> None:
    """
    Logs a taken trade interactively.

    Pulls entry/stop/targets from today's scan log automatically.
    Falls back to manual input if the ticker wasn't scanned today.
    """
    from analytics.scan_logger     import load_scan_log
    from analytics.outcome_tracker import record_trade_entry
    import pandas as pd
    from datetime import date

    # Colour helpers (inline to avoid importing daily_mode)
    _G, _Y, _B, _D, _RST = "\033[92m", "\033[93m", "\033[1m", "\033[2m", "\033[0m"
    g = lambda s: f"{_G}{s}{_RST}"
    b = lambda s: f"{_B}{s}{_RST}"
    d = lambda s: f"{_D}{s}{_RST}"

    print(f"\n  {b('RECORD TRADE')}  —  {ticker}\n")

    scan_df   = load_scan_log(journal_dir)
    today_str = date.today().strftime("%Y-%m-%d")

    match = pd.DataFrame()
    if not scan_df.empty and "scan_date" in scan_df.columns:
        match = scan_df[
            (scan_df["scan_date"] == today_str) &
            (scan_df["ticker"]    == ticker)
        ].tail(1)

    if not match.empty:
        row  = match.iloc[0]
        plan = {k: row.get(k, 0) for k in [
            "ticker", "grade", "score", "entry_price", "stop_price",
            "t1", "t2", "rr_t1", "quantity", "max_loss_inr",
            "atr_pct", "relative_strength", "range_pct",
        ]}
        plan["ticker"] = ticker
        regime = str(row.get("regime", "BULL"))

        print(f"  Found in today's scan log:\n")
        print(f"    Grade  {plan['grade']}   Score {plan['score']}")
        print(f"    Entry  ₹{plan['entry_price']:,.2f}   "
              f"Stop ₹{plan['stop_price']:,.2f}")
        print(f"    T1     ₹{plan['t1']:,.2f}   "
              f"T2   ₹{plan['t2']:,.2f}")
        print(f"    RR     {plan['rr_t1']:.1f}x   "
              f"Qty  {plan['quantity']:,} shares\n")
    else:
        print(f"  {ticker} not in today's scan log. Run --today first if possible.\n")
        try:
            plan = {
                "ticker":            ticker,
                "grade":             input("  Grade (A+/A/B):   ").strip() or "A",
                "score":             float(input("  Score (0–100):    ").strip() or "55"),
                "entry_price":       float(input("  Entry price ₹:    ").strip()),
                "stop_price":        float(input("  Stop price  ₹:    ").strip()),
                "t1":                float(input("  T1 target   ₹:    ").strip()),
                "t2":                float(input("  T2 target   ₹:    ").strip()),
                "quantity":          int(input(  "  Quantity (shares):" ).strip() or "0"),
                "rr_t1":             0.0,
                "max_loss_inr":      0.0,
                "atr_pct":           0.0,
                "relative_strength": 0.0,
                "range_pct":         0.0,
            }
            ep, sp, t1 = plan["entry_price"], plan["stop_price"], plan["t1"]
            plan["rr_t1"] = round((t1 - ep) / (ep - sp), 2) if ep != sp else 0.0
            regime = input("  Regime (BULL/NEUTRAL/BEAR): ").strip() or "BULL"
        except (ValueError, KeyboardInterrupt):
            print("\n  Cancelled.\n")
            return

    confirm = input(f"\n  Confirm: record trade for {ticker}? [y/N]: ").strip().lower()
    if confirm == "y":
        trade_id = record_trade_entry(plan, regime, notes=notes, journal_dir=journal_dir)
        print(g(f"\n  ✓  Recorded  —  trade_id: {trade_id}"))
        print(d(f"     Resolve outcomes later with: python run.py --resolve"))
    else:
        print("  Cancelled.\n")


def _cmd_resolve(journal_dir: str, force: bool) -> None:
    """Auto-resolve open trade outcomes via yfinance price history."""
    from analytics.outcome_tracker import resolve_outcomes

    _G, _B, _RST = "\033[92m", "\033[1m", "\033[0m"
    print(f"\n  {_B}RESOLVING TRADE OUTCOMES{_RST}  "
          f"{'(all trades)' if force else '(open trades only)'}\n")
    resolve_outcomes(journal_dir=journal_dir, force_recheck=force)


def _cmd_report(report_type: str, journal_dir: str) -> None:
    """Weekly / monthly analytics review."""
    from analytics.reports import (
        print_expectancy_report,
        print_segment_report,
        print_equity_report,
        print_scan_log_summary,
        print_full_report,
        print_edge_intelligence_report,
    )
    dispatch = {
        "full":        print_full_report,
        "expectancy":  print_expectancy_report,
        "segment":     print_segment_report,
        "edge":        print_edge_intelligence_report,
        "equity":      print_equity_report,
        "scanlog":     print_scan_log_summary,
    }
    fn = dispatch.get(report_type.strip().lower(), print_full_report)
    fn(journal_dir=journal_dir)

    # Auto-send weekly pulse on Sundays (once per day)
    try:
        from datetime import date
        import calendar
        if date.today().weekday() == 6:  # Sunday
            _cmd_weekly_pulse(journal_dir, force=False)
    except Exception:
        pass


def _cmd_weekly_pulse(journal_dir: str, force: bool = True) -> None:
    """Send weekly performance pulse to Telegram."""
    from integrations.telegram_notifier import (
        notify_weekly_pulse, weekly_pulse_already_sent_today,
    )
    if not force and weekly_pulse_already_sent_today(journal_dir):
        print("  Weekly pulse already sent today.")
        return
    try:
        from analytics.reports import _load_trades  # internal helper
        import pandas as pd
        df = _load_trades(journal_dir)
        # Closed trades only
        closed = df[df["resolved"].astype(str).str.upper() == "TRUE"].copy()
        # This-week trades
        today = pd.Timestamp.today().normalize()
        week_start = today - pd.Timedelta(days=today.dayofweek)
        week = closed[pd.to_datetime(
            closed.get("exit_date", closed.get("entry_date", pd.NaT)),
            errors="coerce"
        ) >= week_start]
        wins   = int((week.get("r_multiple", pd.Series(dtype=float)) > 0).sum())
        losses = int((week.get("r_multiple", pd.Series(dtype=float)) <= 0).sum())
        net_r  = round(float(week.get("r_multiple", pd.Series(dtype=float)).sum()), 2)
        # Rolling 20-trade expectancy
        last20     = closed.tail(20)
        exp_20     = round(float(last20.get("r_multiple",
                            pd.Series(dtype=float)).mean()), 2) if len(last20) >= 5 else 0.0
        # Open trades heat
        open_trades = df[df["resolved"].astype(str).str.upper() != "TRUE"]
        open_count  = len(open_trades)
        from config.config import CONFIG
        cap = float(CONFIG.get("account_capital", 100_000))
        risk_col = open_trades.get("max_loss_inr", pd.Series(dtype=float))
        cap_risk = round(float(risk_col.sum()), 0)
    except Exception:
        wins, losses, net_r, exp_20 = 0, 0, 0.0, 0.0
        open_count, cap_risk = 0, 0.0

    notify_weekly_pulse(
        wins=wins, losses=losses, net_r=net_r,
        expectancy_20=exp_20, open_count=open_count,
        capital_at_risk_inr=cap_risk,
        journal_dir=journal_dir,
    )
    print("  ✓ Weekly pulse sent to Telegram.")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()
