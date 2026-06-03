"""
algo/executor.py — Trade Execution Layer
=========================================
Routes signals through risk check → position sizing → paper/live execution.
Logs all trades to journal/algo_trades.csv and journal/algo_signals.csv.
Telegram notifications skipped in sim_mode or when firewall blocks it.
"""

from __future__ import annotations
import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

from algo.config import ALGO_CONFIG
from algo.risk_guard import RiskGuard, TradeRecord
from algo.strategies import Signal, SignalType


# ANSI colour helpers
_G, _Y, _R, _C, _B, _D, _RST = (
    "\033[92m", "\033[93m", "\033[91m",
    "\033[96m", "\033[1m", "\033[2m", "\033[0m",
)


class AlgoExecutor:
    """
    Executes signals in paper or live mode.
    Paper mode: logs and prints, no real orders.
    Live mode:  places MIS orders via Kite Connect.
    """

    def __init__(self, risk_guard: RiskGuard, paper_mode: bool = True,
                 journal_dir: str = "journal"):
        self.risk_guard = risk_guard
        self.paper_mode = paper_mode
        self._sim_mode  = False          # Set True during simulation to skip file I/O

        # Playbook / ExpectancyGate (lazy-loaded on first call)
        self._playbook           = None
        self._min_expectancy_r   = 0.30   # minimum avg_r to accept a signal

        journal = Path(journal_dir)
        journal.mkdir(exist_ok=True)

        self._log_path        = journal / "algo_trades.csv"
        self._signal_log_path = journal / "algo_signals.csv"

        self._init_logs()

    def _init_logs(self) -> None:
        if not self._log_path.exists():
            with open(self._log_path, "w", newline="") as f:
                csv.writer(f).writerow([
                    "date", "time", "symbol", "type", "strategy",
                    "entry_price", "stop_loss", "target", "quantity",
                    "exit_price", "exit_time", "pnl", "status", "mode", "reason",
                ])
        if not self._signal_log_path.exists():
            with open(self._signal_log_path, "w", newline="") as f:
                csv.writer(f).writerow([
                    "date", "time", "symbol", "type", "strategy",
                    "price", "stop_loss", "target", "rr", "reason",
                    "action", "reject_reason",
                ])

    # ── Main entry points ──────────────────────────────────────────────────

    def execute_signal(self, signal: Signal) -> Optional[TradeRecord]:
        """Risk gate → expectancy gate → sizing → execute. Returns TradeRecord or None."""
        verdict = self.risk_guard.check_new_trade(signal, signal.timestamp)
        if not verdict:
            self._log_signal(signal, "REJECTED", verdict.reason)
            self._print_rejection(signal, verdict.reason)
            return None

        # Expectancy gate: skip if we have sufficient data and edge is negative
        exp_ok, exp_reason = self._gate_expectancy(signal)
        if not exp_ok:
            self._log_signal(signal, "REJECTED", exp_reason)
            self._print_rejection(signal, exp_reason)
            return None

        quantity = self.risk_guard.calculate_position_size(signal)
        if quantity <= 0:
            reason = "Position size = 0 (risk too wide or capital exhausted)"
            self._log_signal(signal, "REJECTED", reason)
            self._print_rejection(signal, reason)
            return None

        signal.quantity = quantity

        if self.paper_mode:
            return self._paper_execute(signal)
        else:
            return self._live_execute(signal)

    def execute_exit(self, trade: TradeRecord, exit_price: float,
                     exit_time: datetime, reason: str = "") -> None:
        if self.paper_mode:
            self._paper_exit(trade, exit_price, exit_time, reason)
        else:
            self._live_exit(trade, exit_price, exit_time, reason)

    def execute_square_off(self, prices: dict[str, float],
                           current_time: datetime) -> list[TradeRecord]:
        closed = self.risk_guard.square_off_all(prices, current_time)
        for trade in closed:
            self._log_trade_exit(trade, "EOD_SQUAREOFF")
            self._print_exit(trade, "EOD SQUARE-OFF")
        return closed

    # ── Paper execution ────────────────────────────────────────────────────

    def _paper_execute(self, signal: Signal) -> TradeRecord:
        trade = self.risk_guard.record_entry(signal, signal.quantity)
        self._log_signal(signal, "FILLED_PAPER", "")
        self._log_trade_entry(trade)
        self._print_entry(signal, trade, paper=True)
        self._telegram_entry(signal, trade, paper=True)
        return trade

    def _paper_exit(self, trade: TradeRecord, exit_price: float,
                    exit_time: datetime, reason: str) -> None:
        self.risk_guard.record_exit(trade.symbol, exit_price, exit_time, reason)
        self._log_trade_exit(trade, reason)
        self._print_exit(trade, reason)
        self._telegram_exit(trade, reason)

    # ── Live execution ────────────────────────────────────────────────────

    def _live_execute(self, signal: Signal) -> Optional[TradeRecord]:
        try:
            from integrations.zerodha import get_kite
            kite = get_kite()

            direction = (kite.TRANSACTION_TYPE_BUY
                         if signal.type == SignalType.BUY
                         else kite.TRANSACTION_TYPE_SELL)

            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=ALGO_CONFIG["exchange"],
                tradingsymbol=signal.symbol,
                transaction_type=direction,
                quantity=signal.quantity,
                product=kite.PRODUCT_MIS,
                order_type=kite.ORDER_TYPE_MARKET,
            )

            trade = self.risk_guard.record_entry(signal, signal.quantity)
            self._log_signal(signal, "FILLED_LIVE", "")
            self._log_trade_entry(trade)
            self._print_entry(signal, trade, paper=False)
            self._telegram_entry(signal, trade, paper=False)
            return trade

        except Exception as e:
            reason = f"Order failed: {e}"
            self._print_rejection(signal, reason)
            return None

    def _live_exit(self, trade: TradeRecord, exit_price: float,
                   exit_time: datetime, reason: str) -> None:
        try:
            from integrations.zerodha import get_kite
            kite = get_kite()

            direction = (kite.TRANSACTION_TYPE_SELL if trade.is_long
                         else kite.TRANSACTION_TYPE_BUY)

            kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=ALGO_CONFIG["exchange"],
                tradingsymbol=trade.symbol,
                transaction_type=direction,
                quantity=trade.quantity,
                product=kite.PRODUCT_MIS,
                order_type=kite.ORDER_TYPE_MARKET,
            )
        except Exception as e:
            print(f"  {_R}Exit order failed for {trade.symbol}: {e}{_RST}")

        self.risk_guard.record_exit(trade.symbol, exit_price, exit_time, reason)
        self._log_trade_exit(trade, reason)
        self._print_exit(trade, reason)
        self._telegram_exit(trade, reason)

    # ── Logging ────────────────────────────────────────────────────────────

    def _log_signal(self, signal: Signal, action: str, reject_reason: str) -> None:
        if self._sim_mode:
            return
        try:
            with open(self._signal_log_path, "a", newline="") as f:
                csv.writer(f).writerow([
                    signal.timestamp.strftime("%Y-%m-%d"),
                    signal.timestamp.strftime("%H:%M:%S"),
                    signal.symbol, signal.type.value, signal.strategy,
                    f"{signal.price:.2f}", f"{signal.stop_loss:.2f}",
                    f"{signal.target:.2f}", f"{signal.rr_ratio:.2f}",
                    signal.reason, action, reject_reason,
                ])
        except Exception:
            pass

    def _log_trade_entry(self, trade: TradeRecord) -> None:
        if self._sim_mode:
            return
        try:
            with open(self._log_path, "a", newline="") as f:
                csv.writer(f).writerow([
                    trade.entry_time.strftime("%Y-%m-%d"),
                    trade.entry_time.strftime("%H:%M:%S"),
                    trade.symbol, trade.signal_type.value, "ORB",
                    f"{trade.entry_price:.2f}", f"{trade.stop_loss:.2f}",
                    f"{trade.target:.2f}", trade.quantity,
                    "", "", "", "OPEN",
                    "PAPER" if self.paper_mode else "LIVE", "",
                ])
        except Exception:
            pass

    def _log_trade_exit(self, trade: TradeRecord, reason: str) -> None:
        if self._sim_mode:
            return
        try:
            with open(self._log_path, "a", newline="") as f:
                csv.writer(f).writerow([
                    trade.entry_time.strftime("%Y-%m-%d"),
                    trade.entry_time.strftime("%H:%M:%S"),
                    trade.symbol, trade.signal_type.value, "ORB",
                    f"{trade.entry_price:.2f}", f"{trade.stop_loss:.2f}",
                    f"{trade.target:.2f}", trade.quantity,
                    f"{trade.exit_price:.2f}" if trade.exit_price else "",
                    trade.exit_time.strftime("%H:%M:%S") if trade.exit_time else "",
                    f"{trade.pnl:.2f}", trade.status,
                    "PAPER" if self.paper_mode else "LIVE", reason,
                ])
        except Exception:
            pass

    # ── Terminal output ────────────────────────────────────────────────────

    def _print_entry(self, signal: Signal, trade: TradeRecord, paper: bool) -> None:
        mode  = "[PAPER]" if paper else "[LIVE]"
        color = _G if signal.type == SignalType.BUY else _R
        side  = "LONG" if signal.type == SignalType.BUY else "SHORT"
        risk  = abs(signal.price - signal.stop_loss) * trade.quantity

        print(f"\n  {_B}{'─' * 60}{_RST}")
        print(f"  {color}{_B}⚡ TRADE ENTRY  {mode}  {signal.timestamp.strftime('%H:%M:%S')}{_RST}")
        print(f"  {_B}{'─' * 60}{_RST}")
        print(f"  {_C}{signal.symbol}{_RST}  {color}{side}{_RST}  \xd7{trade.quantity} shares")
        print(f"  Entry   \u20b9{signal.price:,.2f}")
        print(f"  Stop    \u20b9{signal.stop_loss:,.2f}  (risk \u20b9{risk:,.0f})")
        print(f"  Target  \u20b9{signal.target:,.2f}  (RR {signal.rr_ratio:.1f}x)")
        print(f"  Reason  {signal.reason}")
        print(f"  {_B}{'─' * 60}{_RST}", flush=True)

    def _print_exit(self, trade: TradeRecord, reason: str) -> None:
        color = _G if trade.pnl >= 0 else _R
        emoji = "\u2705" if trade.pnl >= 0 else "\u274c"

        print(f"\n  {_B}{'─' * 60}{_RST}")
        print(f"  {color}{_B}{emoji} TRADE EXIT  "
              f"{trade.exit_time.strftime('%H:%M:%S') if trade.exit_time else ''}{_RST}")
        print(f"  {_B}{'─' * 60}{_RST}")
        print(f"  {_C}{trade.symbol}{_RST}  {trade.signal_type.value}  \xd7{trade.quantity}")
        print(f"  Entry \u20b9{trade.entry_price:,.2f} \u2192 Exit \u20b9{trade.exit_price:,.2f}")
        print(f"  P&L   {color}\u20b9{trade.pnl:+,.2f}{_RST}  ({trade.status})")
        print(f"  Reason: {reason}")
        print(f"  {_B}{'─' * 60}{_RST}", flush=True)

    def _print_rejection(self, signal: Signal, reason: str) -> None:
        print(f"  {_R}\u2717 {signal.symbol} {signal.type.value} rejected: {reason}{_RST}",
              flush=True)

    # ── Expectancy gate ───────────────────────────────────────────────────

    def load_playbook(self) -> None:
        """Lazy-load the Playbook DB.  Call once at engine startup."""
        try:
            from analytics.playbook import Playbook
            self._playbook = Playbook()
        except Exception as exc:
            print(f"  {_D}  [executor] playbook unavailable: {exc}{_RST}")

    def _gate_expectancy(self, signal: Signal) -> tuple[bool, str]:
        """
        Query playbook for avg_r on (symbol, regime) from paper+live trades.
        Blocks the signal only if:
          - playbook is loaded, AND
          - sufficient data exists (>= 5 paper/live trades), AND
          - avg_r < min_expectancy_r (negative or low edge)
        Returns (ok, reason).
        """
        if self._playbook is None:
            return True, ""
        try:
            exp = self._playbook.get_expectancy(
                symbol=signal.symbol,
                regime=getattr(signal, "regime", "UNKNOWN"),
                min_trades=5,
            )
            if not exp["sufficient"]:
                return True, ""   # not enough data — let it through
            avg_r = exp["avg_r"] or 0.0
            if avg_r < self._min_expectancy_r:
                return False, (
                    f"ExpectancyGate: {signal.symbol} [{signal.regime}] "
                    f"avg_r={avg_r:+.2f}R < {self._min_expectancy_r:.2f}R "
                    f"({exp['n']} paper/live trades)"
                )
        except Exception:
            pass
        return True, ""

    # ── Telegram ──────────────────────────────────────────────────────────

    def _telegram_entry(self, signal: Signal, trade: TradeRecord, paper: bool) -> None:
        if self._sim_mode:
            return
        try:
            from integrations.telegram_notifier import _send
            mode = "PAPER" if paper else "LIVE"
            side = "LONG" if signal.type == SignalType.BUY else "SHORT"
            _send(
                f"{'[PAPER]' if paper else '[LIVE]'} ALGO ENTRY\n\n"
                f"{signal.symbol} \u2014 {side}\n"
                f"Entry: \u20b9{signal.price:,.2f}\n"
                f"Stop:  \u20b9{signal.stop_loss:,.2f}\n"
                f"Target:\u20b9{signal.target:,.2f}\n"
                f"Qty: {trade.quantity} | RR: {signal.rr_ratio:.1f}x\n"
                f"Time: {signal.timestamp.strftime('%H:%M:%S')}"
            )
        except Exception:
            pass

    def _telegram_exit(self, trade: TradeRecord, reason: str) -> None:
        if self._sim_mode:
            return
        try:
            from integrations.telegram_notifier import _send
            emoji = "\u2705" if trade.pnl >= 0 else "\u274c"
            _send(
                f"{emoji} ALGO EXIT\n\n"
                f"{trade.symbol} \u2014 {trade.signal_type.value}\n"
                f"Entry: \u20b9{trade.entry_price:,.2f} \u2192 "
                f"Exit: \u20b9{trade.exit_price:,.2f}\n"
                f"P&L: \u20b9{trade.pnl:+,.2f} ({trade.status})\n"
                f"Reason: {reason}"
            )
        except Exception:
            pass

    # ── Summary ────────────────────────────────────────────────────────────

    def get_session_summary(self) -> dict:
        status = self.risk_guard.get_status()
        return {
            "mode":           "PAPER" if self.paper_mode else "LIVE",
            "realized_pnl":   status["realized_pnl"],
            "daily_pnl":      status["daily_pnl"],
            "total_trades":   status["total_trades"],
            "wins":           self.risk_guard.wins,
            "losses":         self.risk_guard.losses,
            "open_positions": status["open_positions"],
            "risk_status":    status,
        }
