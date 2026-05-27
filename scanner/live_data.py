"""
scanner/live_data.py — Live market data abstraction
=====================================================
Single function: `enrich_with_live(tickers)` returns a dict of
live price snapshots — keyed by ticker (e.g. "RELIANCE.NS").

Behavior:
  • If Kite session is active AND market is open      → Kite LTP/quote
  • Otherwise (off-hours or no session)               → yfinance EOD close
  • Either way: callers always get a dict back, never crash.

Output shape (always present, may be zero):
    {
      "ltp":        float,   # last traded price
      "change_pct": float,   # % change vs prev close (Kite only; 0 for yf)
      "day_open":   float,
      "day_high":   float,
      "day_low":    float,
      "source":     "kite" | "yfinance" | "stale",
      "age_sec":    int,     # 0 for kite, EOD-age for yfinance
    }
"""

from __future__ import annotations
from datetime import datetime, time
from typing import Iterable

import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# Market hours (IST). 09:15 – 15:30 weekdays.
# ─────────────────────────────────────────────────────────────────────────────
def _market_is_open(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    if now.weekday() >= 5:           # Sat / Sun
        return False
    t = now.time()
    return time(9, 15) <= t <= time(15, 30)


# ─────────────────────────────────────────────────────────────────────────────
# yfinance fallback (single-shot batched fetch, no per-ticker loop)
# ─────────────────────────────────────────────────────────────────────────────
def _yfinance_snapshot(tickers: list[str]) -> dict[str, dict]:
    if not tickers:
        return {}
    try:
        import yfinance as yf
    except ImportError:
        return {}

    out: dict[str, dict] = {}
    try:
        df = yf.download(tickers, period="2d", auto_adjust=True,
                          progress=False, group_by="ticker", threads=True)
    except Exception:
        return {}
    if df is None or df.empty:
        return {}

    for tk in tickers:
        try:
            sub = df[tk] if isinstance(df.columns, pd.MultiIndex) else df
            sub = sub.dropna()
            if sub.empty:
                continue
            last  = float(sub["Close"].iloc[-1])
            high  = float(sub["High"].iloc[-1])
            low   = float(sub["Low"].iloc[-1])
            opn   = float(sub["Open"].iloc[-1])
            if last <= 0:
                continue
            out[tk] = {
                "ltp":        last,
                "change_pct": 0.0,
                "day_open":   opn,
                "day_high":   high,
                "day_low":    low,
                "source":     "yfinance",
                "age_sec":    0,
            }
        except (KeyError, IndexError, ValueError):
            continue
    return out


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC: enrich_with_live
# ─────────────────────────────────────────────────────────────────────────────
def enrich_with_live(tickers: Iterable[str],
                     prefer_kite: bool = True) -> dict[str, dict]:
    """
    Returns live-price dict keyed by ticker. Empty dict if no source works.
    """
    ticker_list = sorted({str(t).strip() for t in tickers if t})
    if not ticker_list:
        return {}

    # ── Try Kite (only when market is open OR caller explicitly wants it) ──
    if prefer_kite:
        try:
            from integrations.zerodha import get_client_or_none
            kc = get_client_or_none()
        except ImportError:
            kc = None
        if kc is not None and _market_is_open():
            quotes = kc.quote_batch(ticker_list)
            if quotes:
                out: dict[str, dict] = {}
                for tk, q in quotes.items():
                    out[tk] = {
                        "ltp":        q["ltp"],
                        "change_pct": q["change_pct"],
                        "day_open":   q["open"],
                        "day_high":   q["high"],
                        "day_low":    q["low"],
                        "source":     "kite",
                        "age_sec":    0,
                    }
                # Fall back to yfinance for any tickers Kite didn't return
                missing = [t for t in ticker_list if t not in out]
                if missing:
                    out.update(_yfinance_snapshot(missing))
                return out

    # ── Fallback: yfinance EOD ─────────────────────────────────────────────
    return _yfinance_snapshot(ticker_list)


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: data-source banner for display
# ─────────────────────────────────────────────────────────────────────────────
def data_source_label(snap: dict) -> str:
    """One-word label suitable for cockpit headers."""
    if not snap:
        return "EOD"
    src = snap.get("source", "EOD")
    if src == "kite":
        return "LIVE"
    if src == "yfinance":
        return "EOD"
    return src.upper()
