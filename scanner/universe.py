"""scanner/universe.py
Utilities to inspect and compare the configured watchlist and the
backtest-validated (qualified) universe cached in Journal/qualified_universe.json.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Iterable


def _load_watchlist() -> list[str]:
    try:
        from scanner.watchlist import WATCHLIST
        return [t.upper() for t in WATCHLIST]
    except Exception:
        return []


def _load_qualified(journal_dir: str = "Journal") -> dict:
    p = Path(journal_dir) / "qualified_universe.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def show_universe(journal_dir: str = "Journal", max_display: int = 40) -> int:
    """Prints a short report comparing the watchlist and qualified universe.

    Returns 0 on success.
    """
    watch = _load_watchlist()
    cache = _load_qualified(journal_dir)
    qualified = [t.upper() for t in cache.get("qualified", [])]

    print()
    print(f"Watchlist: {len(watch)} tickers")
    if watch:
        print("  " + ", ".join(watch[:max_display]) + ("..." if len(watch) > max_display else ""))
    else:
        print("  (no watchlist found)")

    print()
    if not qualified:
        print("Qualified universe: (none cached) — run: python run.py --swing-backtest --days 180")
        return 0

    print(f"Qualified universe: {len(qualified)} tickers (cached)")
    print("  " + ", ".join(qualified[:max_display]) + ("..." if len(qualified) > max_display else ""))

    inter = sorted(set(watch).intersection(qualified))
    only_watch = sorted(set(watch) - set(qualified))
    only_qual = sorted(set(qualified) - set(watch))

    print()
    print(f"In both watchlist & qualified: {len(inter)}")
    if inter:
        print("  " + ", ".join(inter[:max_display]) + ("..." if len(inter) > max_display else ""))

    print()
    print(f"Watchlist but NOT qualified: {len(only_watch)}")
    if only_watch:
        print("  " + ", ".join(only_watch[:max_display]) + ("..." if len(only_watch) > max_display else ""))

    if only_qual:
        print()
        print(f"Qualified but NOT in watchlist: {len(only_qual)}")
        print("  " + ", ".join(only_qual[:max_display]) + ("..." if len(only_qual) > max_display else ""))

    print()
    print(f"Cache generated at: {cache.get('generated_at', '?')}  |  backtest days: {cache.get('days', '?')}")
    return 0
