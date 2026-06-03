"""
analytics/playbook.py — Institutional Trade Memory  (SQLite)
=============================================================
Records every completed trade with full regime/filter/outcome context
so you can query your historical edge before placing the next order.

Database: Journal/playbook.db

Tables
------
trades            Every completed trade (backtest | paper | live).
override_journal  Manual intervention records (what you overrode and why).

Auto-classification labels
--------------------------
  A+          net_pnl > 0 AND exit was T1+T2 / T1 (full target hit)
  B           net_pnl > 0 AND exit was EOD (still profitable at close)
  BE          |net_pnl| < 2× fees (breakeven — noise)
  TRAP        net_pnl < 0 AND exit was SL
  TREND_FADE  net_pnl < 0 AND regime was CHOP (traded against the environment)

CLI
---
  python run.py --playbook          # full expectancy report
  python run.py --replay 2026-01-15 # all trades on that date with labels
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

_DEFAULT_DB = Path("Journal") / "playbook.db"

# ─────────────────────────────────────────────────────────────────────────────
# DATA TYPES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PlaybookTrade:
    """Full context for one completed trade."""
    # Identity
    source:         str           # backtest | paper | live
    trade_date:     date
    symbol:         str
    direction:      str           # LONG | SHORT

    # Prices & sizing
    entry_price:    float
    exit_price:     float
    stop_price:     float
    quantity:       int
    t1_price:       float = 0.0
    t2_price:       float = 0.0

    # Outcome
    gross_pnl:      float = 0.0
    fees:           float = 0.0
    net_pnl:        float = 0.0
    exit_reason:    str   = ""    # SL / T1+T2 / T1+BE / T1+EOD / T1 / EOD
    r_multiple:     float = 0.0   # realised R = net_pnl / (risk × qty)
    setup_label:    str   = ""    # A+ / B / BE / TRAP / TREND_FADE (auto)

    # Market context
    regime:         str   = "UNKNOWN"   # MarketRegime.value
    breakout_score: float = 0.0         # 0–100 (RVOL-proxy)
    rvol:           float = 0.0
    orb_range_pct:  float = 0.0
    nifty_range_pct: float = 0.0

    # Override tracking
    override:       bool  = False
    override_reason: str  = ""


def _auto_label(t: PlaybookTrade) -> str:
    """Classify a trade based on its outcome."""
    noise_threshold = max(t.fees * 2, 50.0)   # ignore tiny moves
    if abs(t.net_pnl) < noise_threshold:
        return "BE"
    if t.net_pnl > 0 and t.exit_reason in ("T1+T2", "T1", "TARGET"):
        return "A+"
    if t.net_pnl > 0:
        return "B"
    if t.net_pnl < 0 and t.regime == "CHOP":
        return "TREND_FADE"
    if t.net_pnl < 0 and t.exit_reason == "SL":
        return "TRAP"
    return "B" if t.net_pnl >= 0 else "TRAP"


# ─────────────────────────────────────────────────────────────────────────────
# PLAYBOOK
# ─────────────────────────────────────────────────────────────────────────────

class Playbook:
    """Persistent trade memory with expectancy analysis."""

    def __init__(self, db_path: Path | str = _DEFAULT_DB):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._conn() as cx:
            cx.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                source           TEXT    NOT NULL DEFAULT 'backtest',
                trade_date       TEXT    NOT NULL,
                symbol           TEXT    NOT NULL,
                direction        TEXT    NOT NULL,
                entry_price      REAL    NOT NULL,
                exit_price       REAL    NOT NULL DEFAULT 0,
                stop_price       REAL    NOT NULL DEFAULT 0,
                t1_price         REAL    DEFAULT 0,
                t2_price         REAL    DEFAULT 0,
                quantity         INTEGER NOT NULL DEFAULT 1,
                gross_pnl        REAL    DEFAULT 0,
                fees             REAL    DEFAULT 0,
                net_pnl          REAL    DEFAULT 0,
                exit_reason      TEXT    DEFAULT '',
                r_multiple       REAL    DEFAULT 0,
                setup_label      TEXT    DEFAULT '',
                regime           TEXT    DEFAULT 'UNKNOWN',
                breakout_score   REAL    DEFAULT 0,
                rvol             REAL    DEFAULT 0,
                orb_range_pct    REAL    DEFAULT 0,
                nifty_range_pct  REAL    DEFAULT 0,
                override         INTEGER DEFAULT 0,
                override_reason  TEXT    DEFAULT '',
                created_at       TEXT    DEFAULT (datetime('now')),
                UNIQUE(source, trade_date, symbol, direction, entry_price, quantity)
            );

            CREATE TABLE IF NOT EXISTS override_journal (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date       TEXT    NOT NULL,
                symbol           TEXT    NOT NULL,
                system_signal    TEXT    NOT NULL,
                your_action      TEXT    NOT NULL,
                reason           TEXT    DEFAULT '',
                emotional_state  TEXT    DEFAULT '',
                outcome_pnl      REAL    DEFAULT 0,
                created_at       TEXT    DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_trades_date    ON trades(trade_date);
            CREATE INDEX IF NOT EXISTS idx_trades_symbol  ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_trades_regime  ON trades(regime);
            CREATE INDEX IF NOT EXISTS idx_trades_label   ON trades(setup_label);
            CREATE INDEX IF NOT EXISTS idx_trades_source  ON trades(source);
            """)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    # ── Write ─────────────────────────────────────────────────────────────────

    def log_trade(self, trade: PlaybookTrade) -> int:
        """Insert a trade (auto-labels it if label is blank). Returns row id."""
        if not trade.setup_label:
            trade.setup_label = _auto_label(trade)

        if not trade.r_multiple:
            risk = abs(trade.entry_price - trade.stop_price) * max(trade.quantity, 1)
            trade.r_multiple = round(trade.net_pnl / risk, 3) if risk > 0 else 0.0

        with self._conn() as cx:
            cur = cx.execute("""
            INSERT OR IGNORE INTO trades (
                source, trade_date, symbol, direction,
                entry_price, exit_price, stop_price, t1_price, t2_price,
                quantity, gross_pnl, fees, net_pnl,
                exit_reason, r_multiple, setup_label,
                regime, breakout_score, rvol, orb_range_pct, nifty_range_pct,
                override, override_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                trade.source, str(trade.trade_date), trade.symbol, trade.direction,
                trade.entry_price, trade.exit_price, trade.stop_price,
                trade.t1_price, trade.t2_price,
                trade.quantity, trade.gross_pnl, trade.fees, trade.net_pnl,
                trade.exit_reason, trade.r_multiple, trade.setup_label,
                trade.regime, trade.breakout_score, trade.rvol,
                trade.orb_range_pct, trade.nifty_range_pct,
                1 if trade.override else 0, trade.override_reason,
            ))
            # rowcount=1 → inserted; rowcount=0 → duplicate (OR IGNORE skipped)
            return cur.rowcount

    def log_override(self, trade_date: date, symbol: str,
                     system_signal: str, your_action: str,
                     reason: str = "", emotional_state: str = "",
                     outcome_pnl: float = 0.0) -> None:
        with self._conn() as cx:
            cx.execute("""
            INSERT INTO override_journal
                (trade_date, symbol, system_signal, your_action,
                 reason, emotional_state, outcome_pnl)
            VALUES (?,?,?,?,?,?,?)
            """, (str(trade_date), symbol, system_signal, your_action,
                  reason, emotional_state, outcome_pnl))

    def clear_source(self, source: str) -> int:
        """Delete all trades from a given source (e.g. clear stale backtest data)."""
        with self._conn() as cx:
            cur = cx.execute("DELETE FROM trades WHERE source = ?", (source,))
            return cur.rowcount

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_expectancy(
        self,
        symbol: str = "",
        regime: str = "",
        min_trades: int = 5,
    ) -> dict:
        """
        Return expectancy stats for symbol+regime combination.
        Used by ExpectancyGate before each live signal.
        Returns {"n", "avg_r", "win_rate", "avg_pnl", "sufficient"}.
        """
        conditions, params = ["source != 'backtest'"], []
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if regime:
            conditions.append("regime = ?")
            params.append(regime)

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        sql = f"""
        SELECT COUNT(*),
               AVG(r_multiple),
               AVG(net_pnl),
               SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END)
        FROM trades {where}
        """
        with self._conn() as cx:
            row = cx.execute(sql, params).fetchone()

        n, avg_r, avg_pnl, wins = row
        n = n or 0
        if n < min_trades:
            return {"n": n, "avg_r": None, "win_rate": None,
                    "avg_pnl": None, "sufficient": False}
        return {
            "n":          n,
            "avg_r":      round(avg_r   or 0, 3),
            "win_rate":   round((wins or 0) / n, 3),
            "avg_pnl":    round(avg_pnl or 0, 2),
            "sufficient": True,
        }

    def total_trade_count(self, source: str = "") -> int:
        sql = "SELECT COUNT(*) FROM trades"
        params = []
        if source:
            sql += " WHERE source = ?"
            params.append(source)
        with self._conn() as cx:
            return cx.execute(sql, params).fetchone()[0]

    def get_trades_on_date(self, trade_date: date) -> list[dict]:
        with self._conn() as cx:
            cx.row_factory = sqlite3.Row
            rows = cx.execute(
                "SELECT * FROM trades WHERE trade_date = ? ORDER BY id",
                (str(trade_date),),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Reports ───────────────────────────────────────────────────────────────

    def print_report(self) -> None:
        """Full expectancy report grouped by regime and symbol."""
        _B, _G, _R, _D, _Y, _RST = (
            "\033[1m", "\033[92m", "\033[91m", "\033[2m", "\033[93m", "\033[0m",
        )
        bt  = self.total_trade_count("backtest")
        pp  = self.total_trade_count("paper")
        lv  = self.total_trade_count("live")
        tot = bt + pp + lv

        print(f"\n  {_B}{'═'*70}{_RST}")
        print(f"  {_B}  ORB PLAYBOOK  —  {tot} trades  "
              f"(backtest: {bt}  paper: {pp}  live: {lv}){_RST}")
        print(f"  {_B}{'═'*70}{_RST}")

        if tot == 0:
            print(f"  {_D}  No trades yet. Run --backtest to populate.{_RST}\n")
            return

        # By regime (paper + live only for expectancy; show all for context)
        print(f"\n  {_B}Expectancy by Regime  (paper + live trades only):{_RST}")
        print(f"  {'─'*66}")
        print(f"  {'REGIME':12s}  {'N':>5}  {'WIN%':>6}  {'AVG_R':>7}  "
              f"{'AVG_NET':>10}  LABELS")
        print(f"  {'─'*66}")
        with self._conn() as cx:
            for regime in ("TREND_UP", "TREND_DOWN", "CHOP", "UNKNOWN"):
                row = cx.execute("""
                    SELECT COUNT(*), AVG(r_multiple),
                           SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END), AVG(net_pnl)
                    FROM trades WHERE regime = ? AND source != 'backtest'
                """, (regime,)).fetchone()
                n, avg_r, wins, avg_pnl = row
                if not n:
                    continue
                wr  = (wins or 0) / n
                clr = _G if (avg_r or 0) > 0 else _R
                labels_rows = cx.execute("""
                    SELECT setup_label, COUNT(*) FROM trades
                    WHERE regime = ? AND source != 'backtest'
                    GROUP BY setup_label ORDER BY COUNT(*) DESC
                """, (regime,)).fetchall()
                labels = "  ".join(f"{lb}:{cnt}" for lb, cnt in labels_rows)
                print(f"  {regime:12s}  {n:>5}  {wr*100:>5.1f}%  "
                      f"{clr}{(avg_r or 0):>+7.2f}R{_RST}  "
                      f"{clr}₹{(avg_pnl or 0):>+9,.0f}{_RST}  {labels}")
        print(f"  {'─'*66}")

        # By symbol (top 12 by trade count — all sources)
        print(f"\n  {_B}By Symbol (all sources, top 12):{_RST}")
        print(f"  {'─'*66}")
        print(f"  {'SYMBOL':12s}  {'N':>5}  {'WIN%':>6}  {'AVG_R':>7}  {'AVG_NET':>10}  SOURCE")
        print(f"  {'─'*66}")
        with self._conn() as cx:
            rows = cx.execute("""
                SELECT symbol, COUNT(*), AVG(r_multiple),
                       SUM(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END),
                       AVG(net_pnl),
                       GROUP_CONCAT(DISTINCT source)
                FROM trades GROUP BY symbol ORDER BY COUNT(*) DESC LIMIT 12
            """).fetchall()
        for sym, n, avg_r, wins, avg_pnl, srcs in rows:
            wr  = (wins or 0) / n
            clr = _G if (avg_r or 0) > 0 else _R
            print(f"  {sym:12s}  {n:>5}  {wr*100:>5.1f}%  "
                  f"{clr}{(avg_r or 0):>+7.2f}R{_RST}  "
                  f"{clr}₹{(avg_pnl or 0):>+9,.0f}{_RST}  "
                  f"{_D}{srcs}{_RST}")
        print(f"  {'─'*66}")

        # Setup label breakdown
        print(f"\n  {_B}Setup Labels  (all sources):{_RST}")
        print(f"  {'─'*40}")
        label_colors = {"A+": _G, "B": _G, "BE": _Y, "TRAP": _R, "TREND_FADE": _R}
        with self._conn() as cx:
            rows = cx.execute("""
                SELECT setup_label, COUNT(*), AVG(net_pnl)
                FROM trades GROUP BY setup_label ORDER BY COUNT(*) DESC
            """).fetchall()
        for lbl, cnt, avg_p in rows:
            clr = label_colors.get(lbl, _D)
            pct = cnt / tot * 100
            print(f"  {clr}{lbl:12s}{_RST}  {cnt:>5}  ({pct:>4.1f}%)  "
                  f"avg ₹{(avg_p or 0):>+9,.0f}")
        print(f"  {'─'*40}")

        # Override analysis
        with self._conn() as cx:
            n_ov = cx.execute("SELECT COUNT(*) FROM override_journal").fetchone()[0]
        if n_ov:
            print(f"\n  {_B}Override Journal: {n_ov} entries{_RST}")
            with self._conn() as cx:
                rows = cx.execute("""
                    SELECT emotional_state, COUNT(*), AVG(outcome_pnl)
                    FROM override_journal
                    GROUP BY emotional_state ORDER BY COUNT(*) DESC
                """).fetchall()
            for state, cnt, avg_out in rows:
                clr = _G if (avg_out or 0) > 0 else _R
                print(f"  {(state or 'unknown'):12s}  {cnt:>4} overrides  "
                      f"avg outcome {clr}₹{(avg_out or 0):>+9,.0f}{_RST}")

        print(f"  {_B}{'═'*70}{_RST}\n")

    def print_replay(self, trade_date: date) -> None:
        """Print all trades on a specific date with labels and context."""
        _B, _G, _R, _D, _Y, _RST = (
            "\033[1m", "\033[92m", "\033[91m", "\033[2m", "\033[93m", "\033[0m",
        )
        label_colors = {"A+": _G, "B": _G, "BE": _Y, "TRAP": _R, "TREND_FADE": _R}
        trades = self.get_trades_on_date(trade_date)

        print(f"\n  {_B}{'═'*70}{_RST}")
        print(f"  {_B}  REPLAY  —  {trade_date.strftime('%d %B %Y')}  ({len(trades)} trades){_RST}")
        print(f"  {_B}{'═'*70}{_RST}")

        if not trades:
            print(f"  {_D}  No trades logged for {trade_date}. "
                  f"Run --backtest to populate.{_RST}\n")
            return

        regime_shown = False
        for t in trades:
            if not regime_shown:
                print(f"  {_D}  Market regime: {_Y}{t['regime']}{_RST}\n")
                regime_shown = True

            net = t["net_pnl"]
            clr = _G if net >= 0 else _R
            lbl_clr = label_colors.get(t["setup_label"], _D)

            print(f"  {_B}{t['symbol']:12s}{_RST}  {t['direction']:5s}  "
                  f"[{_Y}{t['regime']}{_RST}]  "
                  f"label: {lbl_clr}{t['setup_label']}{_RST}  "
                  f"({_D}{t['source']}{_RST})")
            print(f"    entry ₹{t['entry_price']:>9,.2f}  →  "
                  f"exit ₹{t['exit_price']:>9,.2f}   "
                  f"reason: {t['exit_reason']}")
            print(f"    net: {clr}₹{net:>+9,.2f}{_RST}  "
                  f"R: {clr}{t['r_multiple']:>+.2f}R{_RST}  "
                  f"ORB: {t['orb_range_pct']:.2f}%")

        net_total = sum(t["net_pnl"] for t in trades)
        wins      = sum(1 for t in trades if t["net_pnl"] > 0)
        clr       = _G if net_total >= 0 else _R
        print(f"\n  {'─'*50}")
        print(f"  Day total: {clr}₹{net_total:>+9,.2f}{_RST}  "
              f"({wins}/{len(trades)} wins)")
        print(f"  {_B}{'═'*70}{_RST}\n")
