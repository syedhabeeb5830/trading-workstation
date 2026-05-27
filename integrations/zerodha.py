"""
integrations/zerodha.py — Kite Connect API wrapper
====================================================
Thin, focused wrapper around the official `kiteconnect` SDK.

Responsibilities:
  - Read API credentials from .env
  - Manage the daily OAuth dance (request_token → access_token)
  - Cache the access_token in .zerodha_session.json (auto-expires next day)
  - Provide simple .holdings() / .positions() / .orders() accessors

Design notes:
  - Access tokens expire at ~6 AM IST every morning. We re-login lazily:
    if today's session file is missing or stale, we prompt for re-login.
  - We never log or persist API secret — only the short-lived access_token.
  - This module is the ONLY place that talks to Zerodha. Everything else
    consumes a pandas DataFrame from .holdings_df() / .positions_df().
"""

from __future__ import annotations
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

import pandas as pd


SESSION_FILE = Path(".zerodha_session.json")
ENV_FILE     = Path(".env")


# ── Lazy imports so the rest of the workstation runs without the deps ────────
def _load_kite():
    try:
        from kiteconnect import KiteConnect  # type: ignore
        return KiteConnect
    except ImportError:
        sys.exit(
            "  ✗ kiteconnect not installed.\n"
            "    Run: pip install -r requirements.txt"
        )


def _load_dotenv():
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(ENV_FILE)
    except ImportError:
        # python-dotenv missing — fall back to existing env vars
        pass


# ─────────────────────────────────────────────────────────────────────────────
# CREDENTIAL HANDLING
# ─────────────────────────────────────────────────────────────────────────────
def _get_credentials() -> tuple[str, str, str]:
    _load_dotenv()
    api_key    = os.getenv("KITE_API_KEY", "").strip()
    api_secret = os.getenv("KITE_API_SECRET", "").strip()
    redirect   = os.getenv("KITE_REDIRECT_URL", "https://127.0.0.1").strip()

    if not api_key or not api_secret or api_key == "your_api_key_here":
        sys.exit(
            "  ✗ Kite Connect credentials missing.\n"
            "    1. Copy .env.example → .env\n"
            "    2. Fill KITE_API_KEY and KITE_API_SECRET from\n"
            "       https://developers.kite.trade/apps\n"
        )
    return api_key, api_secret, redirect


# ─────────────────────────────────────────────────────────────────────────────
# SESSION CACHE
# ─────────────────────────────────────────────────────────────────────────────
def _load_session() -> Optional[dict]:
    if not SESSION_FILE.exists():
        return None
    try:
        data = json.loads(SESSION_FILE.read_text())
        # Sessions expire daily at ~6 AM IST. If cached date != today, stale.
        if data.get("date") != date.today().isoformat():
            return None
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _save_session(access_token: str, user_id: str) -> None:
    SESSION_FILE.write_text(json.dumps({
        "date":         date.today().isoformat(),
        "access_token": access_token,
        "user_id":      user_id,
        "saved_at":     datetime.now().isoformat(timespec="seconds"),
    }, indent=2))


# ─────────────────────────────────────────────────────────────────────────────
# LOGIN FLOW
# ─────────────────────────────────────────────────────────────────────────────
def login_interactive() -> None:
    """
    One-time-per-day OAuth flow:
      1. Show login URL → user opens in browser → authenticates with Zerodha
      2. Zerodha redirects to KITE_REDIRECT_URL?request_token=XXX&action=login
      3. User pastes the FULL redirected URL back into this CLI
      4. We extract request_token, exchange for access_token, cache it
    """
    KiteConnect = _load_kite()
    api_key, api_secret, _ = _get_credentials()
    kite = KiteConnect(api_key=api_key)

    print("\n  ┌──────────────────────────────────────────────────────────────┐")
    print("  │  KITE CONNECT — DAILY LOGIN                                  │")
    print("  └──────────────────────────────────────────────────────────────┘\n")
    print("  Step 1.  Open this URL in your browser and authenticate:\n")
    print(f"    {kite.login_url()}\n")
    print("  Step 2.  After login, Zerodha redirects to your registered URL")
    print("           (e.g. https://127.0.0.1/?request_token=ABC123&action=login).")
    print("           Copy the ENTIRE redirected URL from your browser address bar.\n")

    pasted = input("  Paste redirected URL here: ").strip()
    if not pasted:
        sys.exit("  ✗ No URL provided. Aborting.")

    # Extract request_token from the pasted URL
    try:
        qs = parse_qs(urlparse(pasted).query)
        request_token = qs.get("request_token", [None])[0]
        if not request_token:
            # Maybe the user pasted just the token
            request_token = pasted.split("request_token=")[-1].split("&")[0].strip()
    except Exception:
        sys.exit("  ✗ Could not parse request_token from URL.")

    if not request_token:
        sys.exit("  ✗ request_token missing from URL.")

    try:
        session_data = kite.generate_session(request_token, api_secret=api_secret)
    except Exception as e:
        sys.exit(f"  ✗ Token exchange failed: {e}")

    access_token = session_data["access_token"]
    user_id      = session_data.get("user_id", "")
    _save_session(access_token, user_id)

    print(f"\n  ✓ Logged in as {user_id}. Session cached until 6 AM tomorrow.\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLIENT
# ─────────────────────────────────────────────────────────────────────────────
class KiteClient:
    """Wraps an authenticated KiteConnect instance. Re-prompts if stale."""

    def __init__(self) -> None:
        KiteConnect = _load_kite()
        session = _load_session()
        if session is None:
            sys.exit(
                "  ✗ No valid Kite session for today.\n"
                "    Run: python run.py --kite-login"
            )
        api_key, _, _ = _get_credentials()
        self._kite = KiteConnect(api_key=api_key)
        self._kite.set_access_token(session["access_token"])
        self.user_id = session["user_id"]

    # ── Raw passthroughs ────────────────────────────────────────────────────
    def holdings(self) -> list[dict]:
        return self._kite.holdings()

    def positions(self) -> dict:
        return self._kite.positions()

    def orders(self) -> list[dict]:
        return self._kite.orders()

    # ── DataFrame helpers (preferred) ───────────────────────────────────────
    def holdings_df(self) -> pd.DataFrame:
        """
        Returns a normalised DataFrame:
          ticker (with .NS suffix), quantity, avg_price, last_price,
          pnl, exchange.
        """
        raw = self.holdings()
        if not raw:
            return pd.DataFrame(columns=["ticker", "quantity", "avg_price",
                                         "last_price", "pnl", "exchange"])
        df = pd.DataFrame(raw)
        df["ticker"] = df.apply(
            lambda r: f"{r['tradingsymbol']}.NS" if r.get("exchange") == "NSE"
                      else f"{r['tradingsymbol']}.BO",
            axis=1,
        )
        return df.rename(columns={
            "average_price": "avg_price",
            "last_price":    "last_price",
            "pnl":           "pnl",
        })[["ticker", "quantity", "avg_price", "last_price", "pnl", "exchange"]]

    def positions_df(self) -> pd.DataFrame:
        """Intraday net positions (today's MIS/CO/BO trades)."""
        raw = self.positions().get("net", [])
        if not raw:
            return pd.DataFrame(columns=["ticker", "quantity", "avg_price",
                                         "last_price", "pnl", "product"])
        df = pd.DataFrame(raw)
        df["ticker"] = df["tradingsymbol"].apply(lambda s: f"{s}.NS")
        return df.rename(columns={
            "average_price": "avg_price",
            "last_price":    "last_price",
        })[["ticker", "quantity", "avg_price", "last_price", "pnl", "product"]]

    # ── Live market data ────────────────────────────────────────────────────
    @staticmethod
    def _to_kite_symbol(ticker: str) -> str:
        """Convert RELIANCE.NS → NSE:RELIANCE, RELIANCE.BO → BSE:RELIANCE."""
        t = str(ticker).strip().upper()
        if t.endswith(".NS"):
            return f"NSE:{t[:-3]}"
        if t.endswith(".BO"):
            return f"BSE:{t[:-3]}"
        return f"NSE:{t}"

    def ltp_batch(self, tickers: list[str]) -> dict[str, dict]:
        """
        Live last-traded-price for many tickers in one API call.
        Returns: {ticker: {"ltp": float, "instrument_token": int}} keyed by
        the ORIGINAL ticker (e.g. "RELIANCE.NS"). Missing tickers are absent.
        """
        if not tickers:
            return {}
        sym_map  = {self._to_kite_symbol(t): t for t in tickers}
        try:
            raw = self._kite.ltp(list(sym_map.keys()))
        except Exception:
            return {}
        out: dict[str, dict] = {}
        for ksym, data in raw.items():
            orig = sym_map.get(ksym)
            if orig is None or not data:
                continue
            out[orig] = {
                "ltp":              float(data.get("last_price", 0) or 0),
                "instrument_token": int(data.get("instrument_token", 0) or 0),
            }
        return out

    def quote_batch(self, tickers: list[str]) -> dict[str, dict]:
        """
        Richer than ltp_batch — also returns day open/high/low, prev close,
        % change, volume. ~5x slower than ltp(). Use when card display needs it.
        """
        if not tickers:
            return {}
        sym_map = {self._to_kite_symbol(t): t for t in tickers}
        try:
            raw = self._kite.quote(list(sym_map.keys()))
        except Exception:
            return {}
        out: dict[str, dict] = {}
        for ksym, data in raw.items():
            orig = sym_map.get(ksym)
            if orig is None or not data:
                continue
            ohlc       = data.get("ohlc", {}) or {}
            last       = float(data.get("last_price", 0) or 0)
            prev_close = float(ohlc.get("close", 0) or 0)
            change_pct = ((last - prev_close) / prev_close * 100) if prev_close else 0.0
            out[orig] = {
                "ltp":        last,
                "open":       float(ohlc.get("open",  0) or 0),
                "high":       float(ohlc.get("high",  0) or 0),
                "low":        float(ohlc.get("low",   0) or 0),
                "prev_close": prev_close,
                "change_pct": round(change_pct, 2),
                "volume":     int(data.get("volume", 0) or 0),
                "ts":         str(data.get("timestamp", "")),
            }
        return out

    # ── Account / margin ────────────────────────────────────────────────────
    def margins_equity(self) -> dict:
        """
        Returns normalised margin info for the equity segment:
          {
            "available_cash":  ₹ free cash
            "net":             ₹ total available (cash + collateral)
            "used":            ₹ used margin
            "live_balance":    ₹ effective capital (net + used + unrealised)
          }
        Falls back to zero values if the API call fails.
        """
        try:
            m = self._kite.margins(segment="equity")
        except Exception:
            return {"available_cash": 0.0, "net": 0.0, "used": 0.0,
                    "live_balance": 0.0}
        avail = m.get("available", {}) or {}
        util  = m.get("utilised",  {}) or {}
        cash  = float(avail.get("cash",         0) or 0)
        net   = float(m.get("net",              0) or 0)
        used  = float(util.get("debits",        0) or 0)
        # Effective capital = whatever is sitting in equity segment right now
        live  = net + used
        return {
            "available_cash": cash,
            "net":            net,
            "used":           used,
            "live_balance":   live,
        }

    # ── GTT (Good-Till-Triggered) orders ────────────────────────────────────
    def place_gtt_buy_stop(self, ticker: str, trigger_price: float,
                            quantity: int, limit_price: float | None = None,
                            last_price: float | None = None) -> int:
        """
        Places a single-leg GTT BUY order on Zerodha. When `last_price` crosses
        `trigger_price`, a CNC limit-buy is placed at `limit_price`
        (defaults to trigger + 0.5%).

        Returns the GTT trigger_id (int). Raises on failure.

        Survives PC restarts — runs on Zerodha's servers.
        """
        ksym = self._to_kite_symbol(ticker)
        exch, tsym = ksym.split(":", 1)
        if last_price is None:
            ltp = self.ltp_batch([ticker]).get(ticker, {}).get("ltp", trigger_price)
            last_price = ltp or trigger_price
        if limit_price is None:
            limit_price = round(trigger_price * 1.005, 1)  # 0.5% buffer

        gtt_id = self._kite.place_gtt(
            trigger_type=self._kite.GTT_TYPE_SINGLE,
            tradingsymbol=tsym,
            exchange=exch,
            trigger_values=[trigger_price],
            last_price=last_price,
            orders=[{
                "transaction_type": self._kite.TRANSACTION_TYPE_BUY,
                "quantity":          int(quantity),
                "order_type":        self._kite.ORDER_TYPE_LIMIT,
                "product":           self._kite.PRODUCT_CNC,
                "price":             float(limit_price),
            }],
        )
        # SDK returns either {"trigger_id": N} or just N depending on version
        if isinstance(gtt_id, dict):
            gtt_id = gtt_id.get("trigger_id") or gtt_id.get("data", {}).get("trigger_id")
        return int(gtt_id)

    def list_gtts(self) -> list[dict]:
        """All GTTs on the account — active, triggered, expired, cancelled."""
        try:
            return self._kite.get_gtts() or []
        except Exception:
            return []

    def delete_gtt(self, trigger_id: int) -> bool:
        """Cancel an existing GTT. Returns True on success."""
        try:
            self._kite.delete_gtt(trigger_id=int(trigger_id))
            return True
        except Exception:
            return False

    # ── Protective exit GTT (sell-stop / sell-target as OCO) ────────────────
    def place_gtt_protective(self, ticker: str, stop_price: float,
                              target_price: float, quantity: int,
                              last_price: float | None = None) -> int:
        """
        Places a GTT-OCO (one-cancels-other) SELL trigger that protects an
        existing CNC holding. When EITHER `stop_price` (down) OR `target_price`
        (up) is crossed, a limit-sell fires; the other leg auto-cancels.

        This is the standard way to bracket a delivery position on Zerodha
        (since CNC doesn't support intraday SL-M).

        Returns the trigger_id. Raises on failure.
        """
        ksym = self._to_kite_symbol(ticker)
        exch, tsym = ksym.split(":", 1)
        if last_price is None:
            ltp = self.ltp_batch([ticker]).get(ticker, {}).get("ltp", 0)
            last_price = ltp or ((stop_price + target_price) / 2.0)

        # Sell-side limits: stop leg slightly below trigger; target leg at trigger
        stop_limit   = round(stop_price   * 0.995, 1)
        target_limit = round(target_price * 0.999, 1)

        gtt_id = self._kite.place_gtt(
            trigger_type=self._kite.GTT_TYPE_OCO,
            tradingsymbol=tsym,
            exchange=exch,
            trigger_values=[float(stop_price), float(target_price)],
            last_price=float(last_price),
            orders=[
                {
                    "transaction_type": self._kite.TRANSACTION_TYPE_SELL,
                    "quantity":          int(quantity),
                    "order_type":        self._kite.ORDER_TYPE_LIMIT,
                    "product":           self._kite.PRODUCT_CNC,
                    "price":             float(stop_limit),
                },
                {
                    "transaction_type": self._kite.TRANSACTION_TYPE_SELL,
                    "quantity":          int(quantity),
                    "order_type":        self._kite.ORDER_TYPE_LIMIT,
                    "product":           self._kite.PRODUCT_CNC,
                    "price":             float(target_limit),
                },
            ],
        )
        if isinstance(gtt_id, dict):
            gtt_id = gtt_id.get("trigger_id") or gtt_id.get("data", {}).get("trigger_id")
        return int(gtt_id)

    def modify_gtt_protective(self, trigger_id: int, ticker: str,
                                stop_price: float, target_price: float,
                                quantity: int,
                                last_price: float | None = None) -> int:
        """
        Replace an existing protective GTT with new stop/target/quantity.
        Implemented as delete-then-place since Kite's modify_gtt API
        signature is fragile. Returns the NEW trigger_id.
        """
        # Best-effort delete; ignore failure (GTT may already be triggered/expired)
        try:
            self.delete_gtt(trigger_id)
        except Exception:
            pass
        return self.place_gtt_protective(
            ticker, stop_price, target_price, quantity, last_price,
        )


# ─────────────────────────────────────────────────────────────────────────────
# MODULE-LEVEL HELPER — graceful "is Kite available?" check
# ─────────────────────────────────────────────────────────────────────────────
def get_client_or_none() -> Optional["KiteClient"]:
    """
    Returns an authenticated KiteClient if a fresh session exists,
    else None. Never crashes, never prompts. Use this from any module
    that wants to OPTIONALLY enrich its output with live data.

        kc = get_client_or_none()
        if kc:
            ltp = kc.ltp_batch(["RELIANCE.NS"])
    """
    # Cheap pre-check: skip the whole load_kite chain if session is stale
    session = _load_session()
    if session is None:
        return None
    try:
        return KiteClient()
    except SystemExit:
        return None
    except Exception:
        return None


def ensure_session_or_login() -> "KiteClient":
    """
    Used by REQUIRED Kite commands (--sync, --reconcile, --place, --gtts).
    If a fresh session exists → returns the client.
    If not → walks the user through --kite-login flow inline, then returns
    the freshly-authenticated client.
    """
    kc = get_client_or_none()
    if kc is not None:
        return kc
    print("\n  ⚠  No active Kite session for today. Starting login flow...\n")
    login_interactive()
    kc = get_client_or_none()
    if kc is None:
        sys.exit("  ✗ Login completed but session still invalid. Aborting.")
    return kc

