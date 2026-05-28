"""
algo/instruments.py — Kite Instrument Token Resolver
======================================================
Maps plain symbols (RELIANCE, HDFCBANK, ...) to Kite instrument tokens
needed for KiteTicker subscription.

Caches the full instrument list daily so we don't download it every run.
"""

from __future__ import annotations
import csv
from datetime import date
from pathlib import Path
from typing import Optional


_CACHE_FILE = Path(".kite_instruments.csv")


def resolve_tokens(symbols: list[str]) -> dict[str, int]:
    """
    Returns {symbol: instrument_token} for each symbol in the list.
    Only includes NSE equities. Missing symbols are omitted.
    """
    instruments = _load_instruments()
    if not instruments:
        return {}

    result = {}
    for row in instruments:
        sym  = str(row.get("tradingsymbol", "")).upper()
        exch = str(row.get("exchange", "")).upper()
        if sym in symbols and exch == "NSE" and row.get("instrument_type") == "EQ":
            try:
                result[sym] = int(row["instrument_token"])
            except (ValueError, KeyError):
                pass

    return result


def _load_instruments() -> list[dict]:
    """Load from cache or download fresh from Kite."""
    if _cache_is_fresh():
        return _read_cache()

    try:
        from integrations.zerodha import get_kite
        kite = get_kite()
        instruments = kite.instruments("NSE")
        _write_cache(instruments)
        return instruments
    except Exception as e:
        print(f"  [instruments] Could not load: {e}")
        return []


def _cache_is_fresh() -> bool:
    if not _CACHE_FILE.exists():
        return False
    mtime_date = date.fromtimestamp(_CACHE_FILE.stat().st_mtime)
    return mtime_date == date.today()


def _read_cache() -> list[dict]:
    rows = []
    with open(_CACHE_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def _write_cache(instruments: list[dict]) -> None:
    if not instruments:
        return
    keys = list(instruments[0].keys()) if instruments else []
    with open(_CACHE_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(instruments)
