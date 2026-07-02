"""
run.py — Swing Trading Workstation
====================================
NOTE (RC1 retirement audit, 2026-06-12): `--today` is DEPRECATED for stock discovery —
it is RS-secondary and saw 0% of the validated screen's Top-10 leaders. Use `--screen`
for discovery, `--portfolio` for sizing, `--review-portfolio` for holdings. `--today` and
its execution commands remain only as a legacy/execution shell. See RETIREMENT_AUDIT.md.

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
    # RC1 hardening: force UTF-8 stdout so box-drawing glyphs in any command
    # don't crash on a Windows cp1252 console when output is piped/redirected.
    try:
        import sys as _s
        _s.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

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
        help="[DEPRECATED for discovery — use --screen] Legacy daily cockpit / execution shell",
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
        "--auto",
        action="store_true",
        help="Use with --kite-login: fully automated pyotp/TOTP login (no browser). "
             "Requires KITE_USER_ID / KITE_PASSWORD / KITE_TOTP_SECRET in .env",
    )
    parser.add_argument(
        "--weight-sweep",
        action="store_true",
        dest="weight_sweep",
        help="Walk-forward OOS sweep of adaptive weights (RS/Sector/Breakout/Trend) "
             "+ 2025 failure diagnostic. Measurement only — adopts nothing automatically.",
    )
    parser.add_argument(
        "--sweep-quick",
        action="store_true",
        dest="sweep_quick",
        help="Use with --weight-sweep: coarse grid (fast)",
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
        action="store_true",
        help="Swing-trading cockpit: Nifty 500 screen → committee verdict + exact orders + exit rules",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Use with --screen: force-refresh the universe + OHLCV cache",
    )
    parser.add_argument(
        "--research",
        action="store_true",
        help="Use with --screen: show the full analyst tables (top-20 board, "
             "extended monitor, adaptive promotions/demotions)",
    )
    parser.add_argument(
        "--portfolio",
        action="store_true",
        help="Portfolio construction: conviction-weighted allocation under regime/sector/correlation limits",
    )
    parser.add_argument(
        "--review-portfolio",
        action="store_true",
        dest="review_portfolio",
        help="Portfolio lifecycle review: HOLD/ADD/REDUCE/EXIT/ROTATE per holding + theme/action labels",
    )
    parser.add_argument(
        "--sync-portfolio",
        action="store_true",
        dest="sync_portfolio",
        help="Sync live holdings + cash from Zerodha (or state/holdings.csv) → state/portfolio_state.json",
    )
    parser.add_argument(
        "--orders",
        action="store_true",
        help="Build capital-aware order card: current state → target portfolio → BUY/SELL/HOLD instructions",
    )
    parser.add_argument(
        "--deploy",
        action="store_true",
        help="Alias for --orders (G7)",
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=None,
        metavar="INR",
        help="Total capital in INR for --orders / --sync-portfolio (overrides config/deployment.yaml)",
    )
    parser.add_argument(
        "--validate-edge",
        action="store_true",
        dest="validate_edge",
        help="Edge validation: walk-forward forward-return analytics + EDGE_SCORE",
    )
    parser.add_argument(
        "--validate-rolling",
        action="store_true",
        dest="validate_rolling",
        help="Rolling edge validation: weekly screens over N years + significance tests (SLOW)",
    )
    parser.add_argument("--years", type=float, default=2.0,
                        help="Use with --validate-rolling: lookback window in years (default 2)")
    parser.add_argument("--every", type=int, default=1, dest="every_weeks",
                        help="Use with --validate-rolling: screen every N weeks (default 1)")
    parser.add_argument("--rsample", type=int, default=0, dest="rsample",
                        help="Use with --validate-rolling: cap universe to N tickers (default full)")
    parser.add_argument(
        "--orb-screen",
        nargs="?",
        const=30,
        type=int,
        dest="orb_screen",
        metavar="DAYS",
        help="ORB instrument screener — rank the 27 algo candidates (intraday ORB strategy)",
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
        "--profile",
        metavar="PROFILE",
        default="balanced",
        choices=["tight", "balanced", "aggressive", "discovery"],
        help="Filter profile: tight | balanced (default) | aggressive | discovery",
    )
    parser.add_argument(
        "--journal",
        metavar="DIR",
        default="journal",
        help="Journal directory (default: journal/)",
    )
    parser.add_argument(
        "--trades",
        action="store_true",
        help="Live trade-journal dashboard: real executions, rolling expectancy, "
             "win rate, profit factor, drawdown, by regime/sector/tier",
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

    elif args.screen:
        import sys as _sys
        _sys.exit(_cmd_swing_screen(force_refresh=args.refresh,
                                    research=args.research))

    elif args.trades:
        import sys as _sys
        _sys.exit(_cmd_trades())

    elif args.portfolio:
        import sys as _sys
        _sys.exit(_cmd_portfolio(force_refresh=args.refresh))

    elif args.review_portfolio:
        import sys as _sys
        _sys.exit(_cmd_review_portfolio(force_refresh=args.refresh))

    elif args.sync_portfolio:
        import sys as _sys
        _sys.exit(_cmd_sync_portfolio(capital=args.capital))

    elif args.orders or args.deploy:
        import sys as _sys
        _sys.exit(_cmd_orders(capital=args.capital, force_refresh=args.refresh))

    elif args.validate_edge:
        import sys as _sys
        _sys.exit(_cmd_validate_edge())

    elif args.validate_rolling:
        import sys as _sys
        _sys.exit(_cmd_validate_rolling(years=args.years, every_weeks=args.every_weeks,
                                        sample=args.rsample))

    elif args.weight_sweep:
        import sys as _sys
        _sys.exit(_cmd_weight_sweep(quick=args.sweep_quick, top_n=args.top_n))

    elif args.orb_screen is not None:
        _cmd_screen(args.orb_screen, top_n=args.top_n)

    elif args.playbook:
        _cmd_playbook()

    elif args.replay is not None:
        _cmd_replay(args.replay)

    elif args.algo is not None:
        _cmd_algo(args.algo, live=args.live, simulate=args.simulate,
                  status=args.status, journal_dir=args.journal,
                  auto_select=not args.no_auto_select, top_n=args.top_n)

    elif args.today:
        _cmd_today(args.journal, profile=args.profile)

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
        _cmd_kite_login(auto=args.auto)

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
        _cmd_today(args.journal, profile=args.profile)


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
    """ORB instrument screener — rank the 27 algo candidates."""
    from algo.backtest import run_screen
    run_screen(days=days, top_n=top_n)


def _cmd_swing_screen(force_refresh: bool = False, research: bool = False) -> int:
    """Swing-trading cockpit — dynamic Nifty 500 screen (Phases 1-5)."""
    from screen.screen_runner import run_screen as run_swing_screen
    return run_swing_screen(force_refresh=force_refresh, research=research)


def _cmd_portfolio(force_refresh: bool = False) -> int:
    """Portfolio construction — conviction-weighted allocation (Phase 9)."""
    from portfolio.portfolio_engine import run_portfolio
    return run_portfolio(force_refresh=force_refresh)


def _cmd_review_portfolio(force_refresh: bool = False) -> int:
    """Portfolio lifecycle review — HOLD/ADD/REDUCE/EXIT/ROTATE (Phase 10B)."""
    from portfolio.lifecycle_engine import run_review_portfolio
    return run_review_portfolio(force_refresh=force_refresh)


def _cmd_sync_portfolio(capital: float = None) -> int:
    """Sync live holdings + cash from Zerodha (or CSV fallback) → state/portfolio_state.json."""
    import sys as _s
    try:
        _s.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    from portfolio.portfolio_state import sync_and_save, load_deployment_config
    from deploy.order_card import render_state_summary

    cfg = load_deployment_config()
    cap = capital if capital is not None else float(cfg.get("capital", 500_000))

    print(f"\n  Syncing portfolio state  (capital: ₹{cap:,.0f})...")
    state, path = sync_and_save(cap)
    # Heartbeat for the @enforce_live_sync(15m) fail-safe — only when we truly
    # saw the live broker account (not CSV/simulated fallback).
    if "ZERODHA_LIVE" in (state.source or ""):
        try:
            from integrations.kite_auth import mark_synced
            mark_synced(source="sync_portfolio")
        except Exception:
            pass
    render_state_summary(state)
    print(f"  Saved → {path}\n")
    return 0


def _cmd_orders(capital: float = None, force_refresh: bool = False) -> int:
    """
    Capital-aware order card:
      screen → target portfolio → current broker state → BUY / SELL / HOLD delta.
    """
    import sys as _s
    try:
        _s.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    from screen.screen_runner import build_screen, BenchmarkUnavailableError, render_benchmark_abort
    from portfolio.portfolio_engine import PortfolioConstructor
    from portfolio.lifecycle_engine import LifecycleManager
    from portfolio.portfolio_state import (load_state, load_deployment_config,
                                           state_to_lifecycle_holdings)
    from deploy.order_card import build_order_card, render_order_card

    cfg            = load_deployment_config()
    cap            = capital if capital is not None else float(cfg.get("capital", 500_000))
    posture        = str(cfg.get("posture", "paper"))
    time_stop_wks  = int(cfg.get("time_stop_weeks", 12))

    print(f"\n  Building order card  (capital: ₹{cap:,.0f}  |  posture: {posture.upper()})...")
    print("  Running screen → portfolio construction → lifecycle review...")

    try:
        res = build_screen(force_refresh=force_refresh, persist=True)
    except BenchmarkUnavailableError as exc:
        render_benchmark_abort(exc)
        return 2

    # Target portfolio (what the system recommends) — same position cap as the
    # --screen committee plan (ONE truth: config/deployment.yaml target_positions)
    snap = PortfolioConstructor(
        risk_per_trade=float(cfg.get("risk_per_trade_pct", 1.0)),
        max_positions=int(cfg.get("target_positions", 5)),
    ).construct(res.act_snap, res.regime, res.feed.data, persist=True)
    print("  ⓘ Order-card levels are raw scan geometry. For calibrated execution "
          "levels + the committee verdict, run: python run.py --screen")

    # Current account state (Kite → CSV → simulated)
    state = load_state(cap)
    print(f"  Holdings source: {state.source}  ({len(state.holdings)} positions)")

    # Lifecycle review for EXISTING holdings (EXIT/REDUCE/ADD/ROTATE signals)
    lc_holdings = state_to_lifecycle_holdings(state)
    lc_snap     = LifecycleManager(time_stop_weeks=time_stop_wks).review(
        lc_holdings, res, persist=False)

    # Compute and render
    card = build_order_card(snap, state, cap, lc_snap=lc_snap, posture=posture)
    render_order_card(card)
    return 0


def _cmd_trades() -> int:
    """Live trade-journal dashboard — your real executions + rolling analytics."""
    import sys as _s
    try:
        _s.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    from analytics import trade_journal as tj
    G, R, Y, B, D, RST = ("\033[92m", "\033[91m", "\033[93m", "\033[1m", "\033[2m", "\033[0m")

    print(f"\n  {D}Syncing live holdings + resolving outcomes...{RST}")
    sync = tj.sync_from_kite()
    res = tj.resolve_open()
    a = tj.analytics()

    print(f"\n  {B}{'═'*64}{RST}")
    print(f"  {B}  LIVE TRADE JOURNAL{RST}   {D}{sync.get('note','')}{RST}")
    print(f"  {B}{'═'*64}{RST}")
    print(f"  {D}  {a['n_total']} trades · {a['n_open']} open · {a['n_closed']} closed"
          f"  (synced {sync.get('synced',0)}, resolved {res} this run){RST}")

    if not a["n_closed"]:
        print(f"\n  {Y}  No closed trades yet — analytics populate as your trades resolve.{RST}")
        print(f"  {D}  Every BUY-TODAY fill is journaled with its full decision context "
              f"(regime, edge mode, conviction, tier, calibrated levels).{RST}\n")
        return 0

    exp = a["expectancy_R"]; ec = G if (exp or 0) > 0 else R
    print(f"\n  {B}  PERFORMANCE (realized){RST}")
    print(f"  {D}  Win rate {a['win_rate']}%  ·  Expectancy {ec}{exp:+.2f}R{RST}"
          f"{D}  ·  Rolling-20 {a['rolling20_expectancy_R']:+.2f}R  ·  "
          f"Profit factor {a['profit_factor']}{RST}")
    print(f"  {D}  Avg win {a.get('avg_win_R')}R · avg loss {a.get('avg_loss_R')}R · "
          f"max drawdown {a['max_dd_pct']}% · avg hold {a['avg_hold']}d{RST}")

    for label, key in (("BY CONVICTION TIER", "by_tier"),
                       ("BY REGIME", "by_regime"), ("BY SECTOR", "by_sector")):
        g = a.get(key, {})
        if not g:
            continue
        print(f"\n  {B}  {label}{RST}")
        for k, v in sorted(g.items(), key=lambda kv: -kv[1]["exp_R"]):
            col = G if v["exp_R"] > 0 else R
            print(f"  {D}  {k:<22s} n={v['n']:>3}  exp {col}{v['exp_R']:+.2f}R{RST}"
                  f"{D}  win {v['win%']:.0f}%{RST}")
    print()
    return 0


def _cmd_validate_edge() -> int:
    """Edge validation — walk-forward forward-return analytics + EDGE_SCORE (Phase 8)."""
    import sys as _s
    try:
        _s.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    from analytics.edge_validation import run_edge_validation, render_edge_report
    print("\n  Running walk-forward edge validation (this replays the screener "
          "across past dates)...")
    snap, _ = run_edge_validation(period="2y", persist=True, export=True)
    render_edge_report(snap)
    from datetime import date as _d
    print(f"  Reports: reports/edge_report_{_d.today().isoformat()}.md (+ .json)\n")
    return 0


def _cmd_validate_rolling(years: float = 2.0, every_weeks: int = 1,
                          sample: int = 0) -> int:
    """Rolling edge validation — weekly screens + significance tests (Improvement 2)."""
    import sys as _s
    try:
        _s.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    from analytics.rolling_validation import run_rolling_validation, render_rolling_report
    print(f"\n  Rolling validation: replaying the screener every {every_weeks} week(s) "
          f"over {years}y\n  (this is SLOW — one full Phase 2-7 replay per screen date)...")
    rep, _ = run_rolling_validation(period="5y", years=years, every_weeks=every_weeks,
                                    sample=sample, persist=True, export=True)
    render_rolling_report(rep)
    from datetime import date as _d
    print(f"  Reports: reports/rolling_report_{_d.today().isoformat()}.md (+ .json)\n")
    return 0


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


def _cmd_today(journal_dir: str, profile: str = "balanced") -> None:
    from scanner.daily_mode import run_daily_mode
    run_daily_mode(journal_dir=journal_dir, profile=profile)

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


def _cmd_kite_login(auto: bool = False) -> None:
    """Kite Connect login. --auto uses stored credentials + pyotp (no browser);
    otherwise the manual paste-the-redirect-URL OAuth flow."""
    if auto:
        import logging
        logging.basicConfig(level=logging.INFO,
                            format="%(levelname)s %(name)s: %(message)s")
        from integrations.kite_auth import auto_login, AutoLoginError
        try:
            res = auto_login()
            print(f"\n  ✓ Auto-login OK for {res.user_id} "
                  f"(session cached, synced {res.synced_at}).\n")
        except AutoLoginError as exc:
            print(f"\n  ✗ Automated login failed: {exc}\n"
                  f"    Fall back to manual: python run.py --kite-login\n")
            import sys as _s
            _s.exit(2)
        return
    from integrations.zerodha import login_interactive
    login_interactive()


def _cmd_weight_sweep(quick: bool = False, top_n: int = 10) -> int:
    """Walk-forward OOS weight sweep + 2025 failure diagnostic (measurement only)."""
    import sys as _s
    try:
        _s.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    from analytics.weight_sweep import run_sweep, render_report
    print(f"\n  Running walk-forward OOS weight sweep "
          f"({'coarse' if quick else 'full'} grid, basket={top_n or 10})...")
    try:
        rep = run_sweep(top_n=top_n or 10, quick=quick, persist=True)
    except (FileNotFoundError, ValueError) as exc:
        print(f"  ✗ {exc}")
        return 1
    render_report(rep)
    from datetime import date as _d
    print(f"  Report: reports/validation/weight_sweep.json  ({_d.today().isoformat()})\n")
    return 0


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
