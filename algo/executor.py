"""
algo/executor.py — Order Execution Layer (Paper + Live)
=========================================================
Handles the actual order placement — either simulated (paper) or real (Kite).

Paper mode:
  - Logs all signals and simulated fills
  - Tracks positions with assumed zero slippage
  - Sends Telegram notifications
  - Perfect for validating strategy before risking capital

Live mode:
  - Places MARKET/LIMIT orders via Kite Connect
  - Monitors order status
  - Falls back to paper logging if order fails
  - Sends Telegram with real fill details
"""

from __future__ import annotations
import json
import os
import csv
from datetime import datetime, date
from pathlib import Path
from typing import Optional

from algo.config import ALGO_CONFIG
from algo.strategies import Signal, SignalType
from algo.risk_guard import RiskGuard, TradeRecord


# ── Colour helpers ────────────────────────────────────────────────────────────
_G, _Y, _R, _C, _B, _D, _RST = (
    "\033[92m", "\033[93m", "\033[91m",
    "\033[96m", "\033[1m", "\033[2m", "\033[0m"
)


class AlgoExecutor:
    """
    Execution layer. Routes orders to paper or live backend.
    Logs every action to CSV for post-session analysis.
    """

    def __init__(self, risk_guard: RiskGuard, paper_mode: bool = True,
                 journal_dir: str = "journal"):
        self.risk_guard = risk_guard
        self.paper_mode = paper_mode
        self.journal_dir = journal_dir
        self._sim_mode = False  # Set True in simulation — skips Telegram
        self._log_path = Path(journal_dir) / "algo_trades.csv"
        self._signal_log_path = Path(journal_dir) / "algo_signals.csv"
        self._ensure_logs()

    def _ensure_logs(self) -> None:
        """Create log files with headers if they don't exist."""
        os.makedirs(self.journal_dir, exist_ok=True)
        if not self._log_path.exists():
            with open(self._log_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "date", "time", "symbol", "type", "strategy",
                    "entry_price", "stop_loss", "target", "quantity",
                    "exit_price", "exit_time", "pnl", "status", "mode", "reason",
                ])
        if not self._signal_log_path.exists():
            with open(self._signal_log_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "date", "time", "symbol", "type", "strategy",
                    "price", "stop_loss", "target", "rr", "reason",
                    "action", "reject_reason",
                ])

    def execute_signal(self, signal: Signal) -> Optional[TradeRecord]:
        """
        Main entry point. Takes a signal through risk check → sizing → execution.
        Returns TradeRecord if trade was taken, None if rejected.
        """
        now = signal.timestamp

        # ── Risk guard gate ───────────────────────────────────────────
        verdict = self.risk_guard.check_new_trade(signal, now)
        if not verdict:
            self._log_signal(signal, "REJECTED", verdict.reason)
            self._print_rejection(signal, verdict.reason)
            return None

        # ── Position sizing ───────────────────────────────────────────
        quantity = self.risk_guard.calculate_position_size(signal)
        if quantity <= 0:
            reason = "Position size = 0 (risk too wide or capital exhausted)"
            self._log_signal(signal, "REJECTED", reason)
            self._print_rejection(signal, reason)
            return None

        signal.quantity = quantity

        # ── Execute ───────────────────────────────────────────────────
        if self.paper_mode:
            return self._paper_execute(signal)
        else:
            return self._live_execute(signal)

    def execute_exit(self, trade: TradeRecord, exit_price: float,
                     exit_time: datetime, reason: str = "") -> None:
        """Execute an exit (paper or live)."""
        if self.paper_mode:
            self._paper_exit(trade, exit_price, exit_time, reason)
        else:
            self._live_exit(trade, exit_price, exit_time, reason)

    def execute_square_off(self, prices: dict[str, float],
                           current_time: datetime) -> list[TradeRecord]:
        """Square off all open positions."""
        closed = self.risk_guard.square_off_all(prices, current_time)
        for trade in closed:
            self._log_trade_exit(trade, "EOD_SQUAREOFF")
            self._print_exit(trade, "EOD SQUARE-OFF")
        return closed

    # ── Paper execution ───────────────────────────────────────────────────────

    def _paper_execute(self, signal: Signal) -> TradeRecord:
        """Simulate order fill at signal price (zero slippage)."""
        trade = self.risk_guard.record_entry(signal, signal.quantity)
        self._log_signal(signal, "FILLED_PAPER", "")
        self._log_trade_entry(trade)
        self._print_entry(signal, trade, paper=True)
        self._telegram_entry(signal, trade, paper=True)
        return trade

    def _paper_exit(self, trade: TradeRecord, exit_price: float,
                    exit_time: datetime, reason: str) -> None:
        """Simulate exit at given price."""
        self.risk_guard.record_exit(trade.symbol, exit_price, exit_time, reason)
        self._log_trade_exit(trade, reason)
        self._print_exit(trade, reason)
        self._telegram_exit(trade, reason)

    # ── Live execution ────────────────────────────────────────────────────────

    def _live_execute(self, signal: Signal) -> Optional[TradeRecord]:
        """Place real order via Kite Connect."""
        try:
            from integrations.zerodha import get_kite
            kite = get_kite()

            transaction = "BUY" if signal.type == SignalType.BUY else "SELL"
            order_id = kite.place_order(
                variety="regular",
                exchange=ALGO_CONFIG["exchange"],
                tradingsymbol=signal.symbol,
                transaction_type=transaction,
                quantity=signal.quantity,
                product=ALGO_CONFIG["product_type"],
                order_type=ALGO_CONFIG["order_type"],
            )

            trade = self.risk_guard.record_entry(signal, signal.quantity)
            self._log_signal(signal, f"FILLED_LIVE (order:{order_id})", "")
            self._log_trade_entry(trade)
            self._print_entry(signal, trade, paper=False)
            self._telegram_entry(signal, trade, paper=False)
            return trade

        except Exception as e:
            reason = f"ORDER FAILED: {e}"
            self._log_signal(signal, "FAILED", reason)
            self._print_rejection(signal, reason)
            return None

    def _live_exit(self, trade: TradeRecord, exit_price: float,
                   exit_time: datetime, reason: str) -> None:
        """Place real exit order via Kite Connect."""
        try:
            from integrations.zerodha import get_kite
            kite = get_kite()

            # Exit is opposite direction
            transaction = "SELL" if trade.is_long else "BUY"
            kite.place_order(
                variety="regular",
                exchange=ALGO_CONFIG["exchange"],
                tradingsymbol=trade.symbol,
                transaction_type=transaction,
                quantity=trade.quantity,
                product=ALGO_CONFIG["product_type"],
                order_type="MARKET",
            )
            self.risk_guard.record_exit(trade.symbol, exit_price, exit_time, reason)
            self._log_trade_exit(trade, reason)
            self._print_exit(trade, reason)
            self._telegram_exit(trade, reason)

        except Exception as e:
            # CRITICAL: exit failed — log and alert
            msg = f"⚠️ EXIT ORDER FAILED for {trade.symbol}: {e}"
            print(f"\n  {_R}{msg}{_RST}")
            self._telegram_alert(msg)

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log_signal(self, signal: Signal, action: str, reject_reason: str) -> None:
        if self._sim_mode:
            return
        with open(self._signal_log_path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                signal.timestamp.strftime("%Y-%m-%d"),
                signal.timestamp.strftime("%H:%M:%S"),
                signal.symbol,
                signal.type.value,
                signal.strategy,
                f"{signal.price:.2f}",
                f"{signal.stop_loss:.2f}",
                f"{signal.target:.2f}",
                f"{signal.rr_ratio:.2f}",
                signal.reason,
                action,
                reject_reason,
            ])

    def _log_trade_entry(self, trade: TradeRecord) -> None:
        if self._sim_mode:
            return
        with open(self._log_path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                trade.entry_time.strftime("%Y-%m-%d"),
                trade.entry_time.strftime("%H:%M:%S"),
                trade.symbol,
                trade.signal_type.value,
                "ORB",
                f"{trade.entry_price:.2f}",
                f"{trade.stop_loss:.2f}",
                f"{trade.target:.2f}",
                trade.quantity,
                "", "", "", "OPEN",
                "PAPER" if self.paper_mode else "LIVE",
                "",
            ])

    def _log_trade_exit(self, trade: TradeRecord, reason: str) -> None:
        if self._sim_mode:
            return
        """Update the last entry for this trade with exit data."""
        # For simplicity, append a new row with full data
        with open(self._log_path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                trade.entry_time.strftime("%Y-%m-%d"),
                trade.entry_time.strftime("%H:%M:%S"),
                trade.symbol,
                trade.signal_type.value,
                "ORB",
                f"{trade.entry_price:.2f}",
                f"{trade.stop_loss:.2f}",
                f"{trade.target:.2f}",
                trade.quantity,
                f"{trade.exit_price:.2f}" if trade.exit_price else "",
                trade.exit_time.strftime("%H:%M:%S") if trade.exit_time else "",
                f"{trade.pnl:.2f}",
                trade.status,
                "PAPER" if self.paper_mode else "LIVE",
                reason,
            ])

    # ── Terminal output ───────────────────────────────────────────────────────

    def _print_entry(self, signal: Signal, trade: TradeRecord, paper: bool) -> None:
        mode = f"{_Y}PAPER{_RST}" if paper else f"{_G}LIVE{_RST}"
        direction = f"{_G}LONG{_RST}" if signal.type == SignalType.BUY else f"{_R}SHORT{_RST}"
        print(f"\n  {'─' * 60}")
        print(f"  {_B}⚡ TRADE ENTRY{_RST}  [{mode}]  {signal.timestamp.strftime('%H:%M:%S')}")
        print(f"  {'─' * 60}")
        print(f"  {_B}{signal.symbol}{_RST}  {direction}  ×{trade.quantity} shares")
        print(f"  Entry   ₹{signal.price:,.2f}")
        print(f"  Stop    ₹{signal.stop_loss:,.2f}  ({_R}risk ₹{signal.risk_per_share * trade.quantity:,.0f}{_RST})")
        print(f"  Target  ₹{signal.target:,.2f}  (RR {signal.rr_ratio:.1f}x)")
        print(f"  Reason  {signal.reason}")
        print(f"  {'─' * 60}")

    def _print_exit(self, trade: TradeRecord, reason: str) -> None:
        pnl_clr = _G if trade.pnl >= 0 else _R
        print(f"\n  {'─' * 60}")
        print(f"  {_B}🏁 TRADE EXIT{_RST}  {trade.exit_time.strftime('%H:%M:%S') if trade.exit_time else ''}")
        print(f"  {'─' * 60}")
        print(f"  {trade.symbol}  {trade.signal_type.value}  ×{trade.quantity}")
        print(f"  Entry ₹{trade.entry_price:,.2f} → Exit ₹{trade.exit_price:,.2f}")
        print(f"  P&L   {pnl_clr}₹{trade.pnl:+,.2f}{_RST}  ({trade.status})")
        print(f"  Reason: {reason}")
        print(f"  {'─' * 60}")

    def _print_rejection(self, signal: Signal, reason: str) -> None:
        print(f"  {_D}✗ {signal.symbol} {signal.type.value} rejected: {reason}{_RST}")

    # ── Telegram ──────────────────────────────────────────────────────────────

    def _telegram_entry(self, signal: Signal, trade: TradeRecord, paper: bool) -> None:
        if self._sim_mode:
            return
        try:
            from integrations.telegram_notifier import _send
            mode = "📝 PAPER" if paper else "⚡ LIVE"
            direction = "LONG 🟢" if signal.type == SignalType.BUY else "SHORT 🔴"
            msg = (
                f"{mode} | ALGO ENTRY\n\n"
                f"{signal.symbol} — {direction}\n"
                f"Entry: ₹{signal.price:,.2f}\n"
                f"Stop: ₹{signal.stop_loss:,.2f}\n"
                f"Target: ₹{signal.target:,.2f}\n"
                f"Qty: {trade.quantity} | RR: {signal.rr_ratio:.1f}x\n"
                f"Strategy: {signal.strategy}\n"
                f"Time: {signal.timestamp.strftime('%H:%M:%S')}"
            )
            _send(msg)
        except Exception:
            pass

    def _telegram_exit(self, trade: TradeRecord, reason: str) -> None:
        if self._sim_mode:
            return
        try:
            from integrations.telegram_notifier import _send
            emoji = "✅" if trade.pnl >= 0 else "❌"
            msg = (
                f"{emoji} ALGO EXIT\n\n"
                f"{trade.symbol} — {trade.signal_type.value}\n"
                f"Entry: ₹{trade.entry_price:,.2f} → Exit: ₹{trade.exit_price:,.2f}\n"
                f"P&L: ₹{trade.pnl:+,.2f} ({trade.status})\n"
                f"Reason: {reason}"
            )
            _send(msg)
        except Exception:
            pass

    def _telegram_alert(self, msg: str) -> None:
        if self._sim_mode:
            return
        try:
            from integrations.telegram_notifier import _send
            _send(f"🚨 ALGO ALERT\n\n{msg}")
        except Exception:
            pass

    # ── Status ────────────────────────────────────────────────────────────────

    def get_session_summary(self) -> dict:
        """Get full session summary for display."""
        closed = self.risk_guard.closed_trades
        return {
            "mode": "PAPER" if self.paper_mode else "LIVE",
            "total_trades": self.risk_guard.total_trades_today,
            "open_positions": len(self.risk_guard.open_positions),
            "wins": self.risk_guard.wins,
            "losses": self.risk_guard.losses,
            "realized_pnl": round(self.risk_guard.realized_pnl, 2),
            "risk_status": self.risk_guard.get_status(),
        }
