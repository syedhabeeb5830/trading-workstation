"""
scanner/earnings_calendar.py — Earnings-event proximity filter
==============================================================
A swing entry placed days before results carries gap risk that no chart shows.
This fetches the next earnings date per ticker (yfinance `.calendar`) and flags a
candidate when results fall inside the holding window.

Design:
  • Network is touched ONLY for the few names asked about (BUY-TODAY candidates),
    never the whole universe.
  • Results cached to cache/earnings.json with a daily TTL — repeat runs are free.
  • Every failure is swallowed → returns None / "" (the screen never breaks because
    a calendar lookup timed out).

Not a scoring input — a risk warning only.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)
_CACHE = Path("cache/earnings.json")
_TTL_DAYS = 1


def _load_cache() -> dict:
    try:
        if _CACHE.exists():
            return json.loads(_CACHE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_cache(c: dict) -> None:
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps(c, indent=2), encoding="utf-8")
    except Exception as exc:
        _log.warning("earnings cache write failed: %s", exc)


def _fetch_next_earnings(ticker: str) -> Optional[str]:
    """Next earnings date as ISO string via yfinance .calendar, or None."""
    try:
        import yfinance as yf
        cal = yf.Ticker(ticker).calendar
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if not ed:
            return None
        nxt = ed[0] if isinstance(ed, (list, tuple)) else ed
        if hasattr(nxt, "date"):
            nxt = nxt.date()
        return nxt.isoformat() if hasattr(nxt, "isoformat") else str(nxt)
    except Exception as exc:
        _log.debug("earnings fetch failed for %s: %s", ticker, exc)
        return None


def next_earnings(ticker: str) -> Optional[str]:
    """Cached next-earnings ISO date for one ticker (daily TTL). None if unknown."""
    cache = _load_cache()
    today = date.today().isoformat()
    hit = cache.get(ticker)
    if hit and hit.get("fetched") == today:
        return hit.get("date")
    nxt = _fetch_next_earnings(ticker)
    cache[ticker] = {"date": nxt, "fetched": today}
    _save_cache(cache)
    return nxt


def earnings_warning(ticker: str, within_days: int = 7) -> str:
    """'⚠ earnings in N days …' when results fall inside the window, else ''."""
    iso = next_earnings(ticker)
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(iso).date()
    except Exception:
        return ""
    days = (d - date.today()).days
    if 0 <= days <= within_days:
        when = "TODAY" if days == 0 else ("tomorrow" if days == 1 else f"in {days}d")
        return f"⚠ earnings {when} ({iso}) — gap risk, prefer to wait until after results"
    return ""
