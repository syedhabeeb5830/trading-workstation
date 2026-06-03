"""
scanner/doctor.py — Pre-flight Deployment Check
================================================
The single most important gate in the workstation.

If `--doctor` fails on any CRITICAL check, the system MUST NOT trade.

  python run.py --doctor

This is the only mandatory command of the day. Run it before --today,
before --place, and after --reconcile. If you find yourself wanting
to skip or override doctor, that itself is the signal to NOT trade.
"""

from __future__ import annotations
import json
import socket
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional


# ── ANSI colours ─────────────────────────────────────────────────────────────
_R, _Y, _G, _C, _D, _B, _RST = (
    "\033[91m", "\033[93m", "\033[92m",
    "\033[96m", "\033[2m",  "\033[1m", "\033[0m",
)


@dataclass
class CheckResult:
    name:     str
    passed:   bool
    critical: bool        # if True and not passed → system blocks trading
    message:  str
    fix_hint: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# INDIVIDUAL CHECKS — each returns CheckResult
# ─────────────────────────────────────────────────────────────────────────────
def _check_internet() -> CheckResult:
    try:
        socket.create_connection(("api.kite.trade", 443), timeout=4).close()
        return CheckResult("Internet connectivity", True, True, "reachable")
    except OSError as e:
        return CheckResult(
            "Internet connectivity", False, True,
            f"unreachable ({e.__class__.__name__})",
            "Check network / VPN before retrying",
        )


def _check_kite_session() -> CheckResult:
    sess_path = Path(".zerodha_session.json")
    if not sess_path.exists():
        return CheckResult(
            "Kite session valid", False, True,
            "no session file",
            "Run: python run.py --kite-login",
        )
    try:
        data = json.loads(sess_path.read_text())
    except Exception:
        return CheckResult(
            "Kite session valid", False, True,
            "session file corrupt",
            "Delete .zerodha_session.json then --kite-login",
        )
    if data.get("date") != date.today().isoformat():
        return CheckResult(
            "Kite session valid", False, True,
            f"stale (cached {data.get('date')})",
            "Run: python run.py --kite-login",
        )
    return CheckResult(
        "Kite session valid", True, True,
        f"user {data.get('user_id', '?')}, expires tomorrow ~06:00",
    )


def _check_playbook_integrity(journal_dir: str) -> CheckResult:
    db_path = Path(journal_dir) / "playbook.db"
    if not db_path.exists():
        return CheckResult(
            "Playbook DB integrity", True, False,
            "no playbook.db yet (first run)",
        )
    try:
        conn = sqlite3.connect(str(db_path))
        cur  = conn.execute("PRAGMA integrity_check;")
        ok   = cur.fetchone()[0]
        rows = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        conn.close()
        if str(ok).lower() != "ok":
            return CheckResult(
                "Playbook DB integrity", False, True,
                f"integrity_check returned: {ok}",
                "Restore from Journal/playbook.db.bak (auto-backup)",
            )
        return CheckResult(
            "Playbook DB integrity", True, False,
            f"OK ({rows} trades)",
        )
    except sqlite3.Error as e:
        return CheckResult(
            "Playbook DB integrity", False, True,
            f"sqlite error: {e}",
            "Inspect Journal/playbook.db manually",
        )


def _backup_playbook(journal_dir: str) -> None:
    """Make a rolling backup of playbook.db. Idempotent. Silent on failure."""
    db = Path(journal_dir) / "playbook.db"
    if not db.exists() or db.stat().st_size == 0:
        return
    bak = Path(journal_dir) / "playbook.db.bak"
    try:
        bak.write_bytes(db.read_bytes())
    except OSError:
        pass


def _check_reconcile_clean(journal_dir: str) -> CheckResult:
    """Light reconcile — compare snapshot vs open trades. No broker call."""
    try:
        from analytics.outcome_tracker import load_trade_log
        df = load_trade_log(journal_dir=journal_dir)
        if df.empty:
            return CheckResult(
                "Journal ↔ Broker reconciliation", True, False,
                "no trades yet",
            )
        open_df = df[df["status"].astype(str).str.upper() == "OPEN"]
        n_open  = len(open_df)
    except Exception as e:
        return CheckResult(
            "Journal ↔ Broker reconciliation", False, False,
            f"could not load trade log ({e.__class__.__name__})",
            "Run: python run.py --reconcile",
        )

    snap_path = Path(journal_dir) / "zerodha_snapshot.json"
    if not snap_path.exists():
        return CheckResult(
            "Journal ↔ Broker reconciliation", False, n_open > 0,
            "no broker snapshot — never synced",
            "Run: python run.py --reconcile",
        )
    try:
        snap = json.loads(snap_path.read_text())
    except Exception:
        return CheckResult(
            "Journal ↔ Broker reconciliation", False, True,
            "snapshot file corrupt",
            "Delete zerodha_snapshot.json and --reconcile",
        )

    fetched = snap.get("fetched_at", "")
    try:
        fetched_dt = datetime.fromisoformat(fetched)
        age_h = (datetime.now() - fetched_dt).total_seconds() / 3600
    except Exception:
        age_h = 999

    if age_h > 36:
        return CheckResult(
            "Journal ↔ Broker reconciliation", False, n_open > 0,
            f"snapshot {age_h:.0f}h old (>36h)",
            "Run: python run.py --reconcile",
        )

    snap_holdings = {h.get("ticker") for h in snap.get("holdings", []) if h.get("ticker")}
    snap_set      = {str(t) for t in snap_holdings}
    journal_set   = set(open_df["ticker"].astype(str).str.upper())

    orphans = snap_set - journal_set
    missing = journal_set - snap_set

    if orphans or missing:
        return CheckResult(
            "Journal ↔ Broker reconciliation", False, True,
            f"{len(orphans)} orphan(s), {len(missing)} missing",
            "Run: python run.py --reconcile",
        )
    return CheckResult(
        "Journal ↔ Broker reconciliation", True, True,
        f"clean ({n_open} open, snapshot {age_h:.1f}h ago)",
    )


def _check_open_positions_have_stops(journal_dir: str) -> CheckResult:
    try:
        from analytics.outcome_tracker import load_trade_log
        df = load_trade_log(journal_dir=journal_dir)
        if df.empty:
            return CheckResult("All open positions have stops", True, True, "no open trades")
        open_df = df[df["status"].astype(str).str.upper() == "OPEN"]
        if open_df.empty:
            return CheckResult("All open positions have stops", True, True, "no open trades")
        no_stop = open_df[open_df["stop_price"].fillna(0) <= 0]
        if len(no_stop):
            tickers = ", ".join(no_stop["ticker"].head(3).tolist())
            return CheckResult(
                "All open positions have stops", False, True,
                f"{len(no_stop)} UNPROTECTED: {tickers}",
                "Run: python run.py --trail TICKER STOP_PRICE",
            )
        return CheckResult(
            "All open positions have stops", True, True,
            f"all {len(open_df)} protected",
        )
    except Exception as e:
        return CheckResult(
            "All open positions have stops", False, True,
            f"check failed: {e.__class__.__name__}",
        )


def _check_portfolio_heat(journal_dir: str, config: dict) -> CheckResult:
    try:
        from analytics.outcome_tracker import load_trade_log
        df = load_trade_log(journal_dir=journal_dir)
        if df.empty:
            return CheckResult("Portfolio heat within limit", True, False, "no open risk")
        open_df = df[df["status"].astype(str).str.upper() == "OPEN"]
        if open_df.empty:
            return CheckResult("Portfolio heat within limit", True, False, "no open risk")
        total_risk = 0.0
        for _, r in open_df.iterrows():
            entry = float(r.get("entry_price", 0) or 0)
            stop  = float(r.get("stop_price",  0) or 0)
            qty   = float(r.get("quantity",    0) or 0)
            if entry > 0 and stop > 0 and qty > 0:
                total_risk += max(entry - stop, 0) * qty
        capital  = float(config.get("account_capital", 100_000))
        max_heat = float(config.get("max_portfolio_heat", 0.03))
        heat_pct = (total_risk / capital * 100) if capital else 0
        limit    = max_heat * 100 if max_heat < 1 else max_heat
        if heat_pct > limit:
            return CheckResult(
                "Portfolio heat within limit", False, True,
                f"{heat_pct:.1f}% > limit {limit:.1f}%",
                "No new trades allowed until heat drops",
            )
        return CheckResult(
            "Portfolio heat within limit", True, False,
            f"{heat_pct:.1f}% / {limit:.1f}%",
        )
    except Exception as e:
        return CheckResult(
            "Portfolio heat within limit", False, False,
            f"check failed: {e.__class__.__name__}",
        )


def _check_daily_loss_circuit(journal_dir: str, config: dict) -> CheckResult:
    """Block new trades if today's realised + unrealised loss exceeds circuit."""
    try:
        from analytics.outcome_tracker import load_trade_log
        df = load_trade_log(journal_dir=journal_dir)
        if df.empty:
            return CheckResult("Daily-loss circuit breaker", True, False, "no trades")
        today = date.today()

        # Realised today
        df["exit_date"] = pd.to_datetime(df.get("exit_date"), errors="coerce")
        closed_today = df[
            (df["status"].astype(str).str.upper() == "CLOSED") &
            (df["exit_date"].dt.date == today)
        ]
        realised = 0.0
        for _, r in closed_today.iterrows():
            entry = float(r.get("entry_price", 0) or 0)
            exitp = float(r.get("exit_price",  0) or 0)
            qty   = float(r.get("quantity",    0) or 0)
            realised += (exitp - entry) * qty
    except Exception as e:
        return CheckResult(
            "Daily-loss circuit breaker", False, False,
            f"check failed: {e.__class__.__name__}",
        )

    capital = float(config.get("account_capital", 100_000))
    breaker = float(config.get("daily_loss_breaker_pct", 0.02))
    breaker_inr = capital * breaker

    if realised <= -breaker_inr:
        return CheckResult(
            "Daily-loss circuit breaker", False, True,
            f"realised ₹{realised:,.0f} ≤ -₹{breaker_inr:,.0f}",
            "NO NEW TRADES today. Resume tomorrow.",
        )
    return CheckResult(
        "Daily-loss circuit breaker", True, False,
        f"realised ₹{realised:+,.0f} (limit -₹{breaker_inr:,.0f})",
    )


def _check_data_freshness() -> CheckResult:
    """yfinance EOD smoke test on NIFTY."""
    try:
        import contextlib, io
        import yfinance as yf
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            df = yf.download("^NSEI", period="5d", interval="1d",
                              progress=False, auto_adjust=False)
        if df is None or len(df) == 0:
            return CheckResult(
                "EOD data freshness", False, False,
                "yfinance returned empty",
                "Network/yfinance issue — try again later",
            )
        last = df.index[-1].date()
        age  = (date.today() - last).days
        if age > 4:
            return CheckResult(
                "EOD data freshness", False, False,
                f"last bar {last} ({age}d old)",
                "Wait for next session close",
            )
        return CheckResult(
            "EOD data freshness", True, False,
            f"NIFTY last bar {last} ({age}d ago)",
        )
    except Exception as e:
        return CheckResult(
            "EOD data freshness", False, False,
            f"yfinance error: {e.__class__.__name__}",
        )


def _check_config_sanity(config: dict) -> CheckResult:
    issues = []
    if float(config.get("max_risk_per_trade", 0)) > 0.02:
        issues.append("max_risk_per_trade > 2% (aggressive)")
    if float(config.get("max_portfolio_heat", 0)) > 0.06:
        issues.append("max_portfolio_heat > 6% (aggressive)")
    if int(config.get("max_positions", 0)) > 5:
        issues.append("max_positions > 5 (over-diversified)")
    if not config.get("daily_loss_breaker_pct"):
        issues.append("daily_loss_breaker_pct missing (no circuit breaker)")
    if issues:
        return CheckResult(
            "Config sanity", False, False,
            "; ".join(issues),
            "Review config/config.py",
        )
    return CheckResult("Config sanity", True, False, "limits within retail bounds")


def _check_no_algo_live_enabled(config: dict) -> CheckResult:
    """Verify algo live mode hasn't been re-enabled."""
    try:
        import inspect
        from algo.engine import run_algo
        src = inspect.getsource(run_algo)
        if "NotImplementedError" in src and "FROZEN" in src:
            return CheckResult("Algo --live frozen", True, False, "algo live disabled")
        return CheckResult(
            "Algo --live frozen", False, True,
            "algo run_algo appears unfrozen",
            "Re-apply freeze in algo/engine.py::run_algo",
        )
    except ImportError:
        return CheckResult("Algo --live frozen", True, False, "algo module not present")
    except Exception:
        return CheckResult("Algo --live frozen", True, False, "verification skipped")


# pandas is imported lazily for the daily-loss check; expose at module scope.
import pandas as pd  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# RUNNER
# ─────────────────────────────────────────────────────────────────────────────
def run_doctor(journal_dir: str = "journal", strict: bool = False) -> int:
    """
    Runs all checks, prints a report, returns exit code:
      0 = all critical checks passed (trading allowed)
      1 = at least one critical check failed (trading BLOCKED)
    """
    from config.config import CONFIG as config

    # Always backup playbook before any other operation
    _backup_playbook(journal_dir)

    checks = [
        _check_internet(),
        _check_kite_session(),
        _check_playbook_integrity(journal_dir),
        _check_reconcile_clean(journal_dir),
        _check_open_positions_have_stops(journal_dir),
        _check_portfolio_heat(journal_dir, config),
        _check_daily_loss_circuit(journal_dir, config),
        _check_data_freshness(),
        _check_config_sanity(config),
        _check_no_algo_live_enabled(config),
    ]

    width = 64
    print()
    print(f"  {_B}╔{'═' * width}╗{_RST}")
    print(f"  {_B}║{'PRE-FLIGHT DEPLOYMENT CHECK'.center(width)}║{_RST}")
    print(f"  {_B}╠{'═' * width}╣{_RST}")

    n_critical_fail = 0
    n_warn          = 0
    for c in checks:
        if c.passed:
            mark, col = "✓", _G
        else:
            mark, col = ("✗", _R) if c.critical else ("!", _Y)
            (n_critical_fail if c.critical else n_warn).__add__(0)  # noqa
            if c.critical:
                n_critical_fail += 1
            else:
                n_warn += 1
        name_line = f"[{col}{mark}{_RST}] {c.name}"
        msg_line  = f"      {_D}{c.message}{_RST}"
        # Pad inside box (visible width only)
        vis = len(c.name) + 4
        pad = max(width - vis, 0)
        print(f"  {_B}║{_RST} {name_line}{' ' * pad}{_B} ║{_RST}")
        vis2 = len(c.message) + 6
        pad2 = max(width - vis2, 0)
        print(f"  {_B}║{_RST} {msg_line}{' ' * pad2}{_B} ║{_RST}")
        if not c.passed and c.fix_hint:
            hint = f"      → {c.fix_hint}"
            vis3 = len(c.fix_hint) + 8
            pad3 = max(width - vis3, 0)
            print(f"  {_B}║{_RST} {_C}{hint}{_RST}{' ' * pad3}{_B} ║{_RST}")

    print(f"  {_B}╠{'═' * width}╣{_RST}")
    n_pass = sum(1 for c in checks if c.passed)
    summary = f"{n_pass}/{len(checks)} pass, {n_warn} warn, {n_critical_fail} critical fail"
    vis = len(summary) + 9
    pad = max(width - vis, 0)
    print(f"  {_B}║{_RST} RESULT: {summary}{' ' * pad}{_B} ║{_RST}")

    if n_critical_fail == 0:
        status = f"{_G}TRADING ALLOWED{_RST}"
    else:
        status = f"{_R}TRADING BLOCKED{_RST}"
    vis = len("STATUS: " + ("TRADING ALLOWED" if n_critical_fail == 0 else "TRADING BLOCKED")) + 1
    pad = max(width - vis, 0)
    print(f"  {_B}║{_RST} STATUS: {status}{' ' * pad}{_B} ║{_RST}")
    print(f"  {_B}╚{'═' * width}╝{_RST}")
    print()

    if n_critical_fail > 0:
        print(f"  {_R}Doctor blocked deployment. Resolve all CRITICAL items.{_RST}\n")
        return 1
    if n_warn > 0 and strict:
        print(f"  {_Y}Warnings present and --strict — blocking.{_RST}\n")
        return 1
    return 0
