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

  PRE-FLIGHT (mandatory first command each morning):
    python run.py --doctor                ← deployment readiness check

  WEEKLY review:
    python run.py --report [TYPE]         ← analytics; TYPE=full|expectancy|
                                            segment|edge|equity|scanlog

  STRATEGY VALIDATION (monthly / pre-deployment):
    python run.py --backtest 60           ← ORB intraday backtest (research)
    python run.py --walkforward 120       ← out-of-sample edge check
    python run.py --swing-backtest        ← per-ticker SWING validation

  ONE-TIME SETUP:
    python run.py --kite-login            ← refresh Kite session (also auto)

  ADVANCED / occasional:
    python run.py --gtts                  ← list all GTTs on the account
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
        "--doctor",
        action="store_true",
        help="Pre-flight deployment check (run FIRST every morning).",
    )
    parser.add_argument(
        "--morning",
        action="store_true",
        help="Zero-decision morning: doctor + scan + ONE trade pick. Default for new users.",
    )
    parser.add_argument(
        "--universe",
        action="store_true",
        help="Show watchlist vs backtest-qualified universe report.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Use with --doctor: treat warnings as failures too.",
    )
    parser.add_argument(
        "--swing-backtest",
        nargs="?",
        const="",
        dest="swing_backtest",
        metavar="TICKERS",
        help="Per-ticker swing backtest. Pass comma-list or omit to use today's shortlist.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=180,
        help="Days of history for --swing-backtest (default: 180).",
    )

    parser.add_argument(
        "--backtest",
        nargs="?",
        const=30,
        type=int,
        metavar="DAYS",
        help="Run ORB backtest over last N trading days (default: 30)."
             " Auto-selects best 8 instruments via screener.",
    )
    parser.add_argument(
        "--walkforward",
        nargs="?",
        const=120,
        type=int,
        metavar="DAYS",
        help="Walk-forward validation: first 80%% = in-sample, last 20%% = OOS (default: 120 days).",
    )
    parser.add_argument(
        "--screen",
        nargs="?",
        const=30,
        type=int,
        metavar="DAYS",
        help="Run instrument screener only — rank all 27 candidates, show top picks",
    )
    parser.add_argument(
        "--no-auto-select",
        action="store_true",
        dest="no_auto_select",
        help="Use with --backtest: skip screener, use hardcoded instruments in config.py",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=8,
        dest="top_n",
        metavar="N",
        help="How many stocks the screener selects (default: 8)",
    )
    parser.add_argument(
        "--playbook",
        action="store_true",
        help="Show expectancy report from Journal/playbook.db",
    )
    parser.add_argument(
        "--replay",
        metavar="DATE",
        default=None,
        help="Show all trades on DATE from playbook (format: YYYY-MM-DD)",
    )
    parser.add_argument(
        "--algo",
        nargs="?",
        const="ORB",
        metavar="STRATEGY",
        help="Run algo engine in paper mode (default: ORB strategy)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use with --algo: place REAL orders (default is paper)",
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Use with --algo: replay yesterday's data through strategy",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Use with --algo: show current session P&L and risk state",
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

    if args.doctor:
        import sys as _sys
        _sys.exit(_cmd_doctor(args.journal, strict=args.strict))

    if args.morning:
        import sys as _sys
        _sys.exit(_cmd_morning(args.journal))

    if args.universe:
        import sys as _sys
        _sys.exit(_cmd_universe(args.journal))

    if args.swing_backtest is not None:
        _cmd_swing_backtest(args.swing_backtest, args.journal, days=args.days)
        return

    if args.backtest is not None:
        _cmd_backtest(args.backtest,
                      auto_select=not args.no_auto_select,
                      top_n=args.top_n)

    elif args.walkforward is not None:
        _cmd_walkforward(args.walkforward)

    elif args.screen is not None:
        _cmd_screen(args.screen, top_n=args.top_n)

    elif args.playbook:
        _cmd_playbook()

    elif args.replay is not None:
        _cmd_replay(args.replay)

    elif args.algo is not None:
        _cmd_algo(args.algo, live=args.live, simulate=args.simulate,
                  status=args.status, journal_dir=args.journal,
                  auto_select=not args.no_auto_select, top_n=args.top_n)

    elif args.today:
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

    elif args.kite_login:
        _cmd_kite_login()

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

def _cmd_backtest(days: int = 30, auto_select: bool = True, top_n: int = 8) -> None:
    """Multi-day ORB backtest with Zerodha fee model."""
    from algo.backtest import run_backtest
    run_backtest(days=days, auto_select=auto_select, top_n=top_n)


def _cmd_walkforward(days: int = 120) -> None:
    """Walk-forward validation: IS (80%) vs OOS (20%) using the FILTERED config."""
    from algo.backtest import run_walkforward
    run_walkforward(days=days)


def _cmd_screen(days: int = 30, top_n: int = 8) -> None:
    """Standalone instrument screener — rank all 27 candidates."""
    from algo.backtest import run_screen
    run_screen(days=days, top_n=top_n)


def _cmd_playbook() -> None:
    """Print full expectancy report from Journal/playbook.db."""
    from analytics.playbook import Playbook
    Playbook().print_report()


def _cmd_replay(date_str: str) -> None:
    """Print all trades on a specific date from the playbook."""
    from datetime import date
    from analytics.playbook import Playbook
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        print(f"  Invalid date '{date_str}'. Use YYYY-MM-DD format.")
        return
    Playbook().print_replay(d)


def _cmd_algo(strategy: str, live: bool = False, simulate: bool = False,
              status: bool = False, journal_dir: str = "journal",
              auto_select: bool = True, top_n: int = 8) -> None:
    """Algo engine — ORB strategy, paper/live/simulate modes."""
    from algo.engine import run_algo
    run_algo(strategy=strategy, live=live, simulate=simulate,
             status=status, journal_dir=journal_dir,
             auto_select=auto_select, top_n=top_n)


def _cmd_today(journal_dir: str) -> None:
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
    print("\n  ✗ --install-scheduler removed in production hardening.\n"
          "    Manual --positions only; auto-checking creates monitoring compulsion.\n")


def _cmd_uninstall_scheduler() -> None:
    print("\n  ✗ --uninstall-scheduler removed (see --install-scheduler).\n")


def _cmd_alerts(journal_dir: str) -> None:
    print("\n  ✗ --alerts removed in production hardening.\n"
          "    Telegram alerts created emotional triggers. Use --positions and --doctor instead.\n")


def _cmd_doctor(journal_dir: str, strict: bool = False) -> int:
    """Pre-flight deployment check."""
    from scanner.doctor import run_doctor
    return run_doctor(journal_dir=journal_dir, strict=strict)


def _cmd_morning(journal_dir: str) -> int:
    """Zero-decision-fatigue morning command."""
    from scanner.morning import run_morning
    return run_morning(journal_dir=journal_dir, run_doctor_first=True,
                       refresh_scan=True)


def _cmd_universe(journal_dir: str) -> int:
    """Show watchlist and qualified-universe comparison report."""
    from scanner.universe import show_universe
    return show_universe(journal_dir=journal_dir)


def _cmd_swing_backtest(spec: str, journal_dir: str, days: int = 180) -> None:
    """Per-ticker swing strategy backtest."""
    spec = (spec or "").strip()
    if not spec:
        from analytics.swing_backtest import run_swing_backtest_shortlist
        run_swing_backtest_shortlist(journal_dir=journal_dir, days=days)
        return
    from analytics.swing_backtest import run_swing_backtest
    tickers = [t.strip() for t in spec.split(",") if t.strip()]
    if not tickers:
        print("  No tickers supplied.")
        return
    run_swing_backtest(tickers, days=days)


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

    # Weekly Telegram pulse removed in production hardening:
    # auto-broadcasts create monitoring compulsion. Use --report locally.


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
