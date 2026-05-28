"""
algo/instruments.py — Kite Instrument Token Resolver
======================================================
Maps trading symbols (e.g. "RELIANCE") to Kite instrument tokens
needed for KiteTicker WebSocket subscription.

Instrument list is cached daily (downloaded once from Kite API).
For simulation mode, tokens aren't needed (uses yfinance).
"""

from __future__ import annotations
import csv
import io
import os
from datetime import date
from pathlib import Path
from typing import Optional

CACHE_FILE = Path(".kite_instruments.csv")


def _download_instruments() -> list[dict]:
    """Download full instrument list from Kite API."""
    try:
        from integrations.zerodha import get_kite
        kite = get_kite()
        instruments = kite.instruments("NSE")
        return instruments
    except Exception as e:
        print(f"  ⚠ Could not download instruments: {e}")
        return []


def _load_cached() -> list[dict]:
    """Load instruments from daily cache file."""
    if not CACHE_FILE.exists():
        return []
    # Check if cache is from today
    mtime = date.fromtimestamp(CACHE_FILE.stat().st_mtime)
    if mtime != date.today():
        return []
    instruments = []
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            instruments.append(row)
    return instruments


def _save_cache(instruments: list[dict]) -> None:
    """Save instruments to daily cache."""
    if not instruments:
        return
    keys = instruments[0].keys()
    with open(CACHE_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(instruments)


def resolve_tokens(symbols: list[str], exchange: str = "NSE") -> dict[str, int]:
    """
    Resolve a list of trading symbols to their instrument tokens.
    Returns: {"RELIANCE": 738561, "HDFCBANK": 341249, ...}
    """
    # Try cache first
    instruments = _load_cached()
    if not instruments:
        instruments = _download_instruments()
        if instruments:
            _save_cache(instruments)

    token_map = {}
    for sym in symbols:
        for inst in instruments:
            # Match on tradingsymbol and exchange
            ts = inst.get("tradingsymbol", "")
            ex = inst.get("exchange", "")
            if ts == sym and ex == exchange:
                try:
                    token_map[sym] = int(inst["instrument_token"])
                except (ValueError, KeyError):
                    pass
                break

    return token_map


def get_instrument_token(symbol: str, exchange: str = "NSE") -> Optional[int]:
    """Get token for a single symbol."""
    result = resolve_tokens([symbol], exchange)
    return result.get(symbol)
