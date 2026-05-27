"""
scanner/capital.py — Live account capital resolution
======================================================
Single function `get_effective_capital(config)` returns the capital
the sizer should use. Tries Zerodha margins("equity") first, falls
back to the config value silently.

Why this matters: after a 10% drawdown, your real Zerodha balance
shrinks but `config.account_capital` doesn't. The position sizer
keeps computing risk against the OLD capital → you take BIGGER
trades after losses. This module fixes that.
"""

from __future__ import annotations
from typing import NamedTuple


class CapitalInfo(NamedTuple):
    capital:     float    # ₹ effective capital used for sizing
    available:   float    # ₹ free cash for new trades
    deployed:    float    # ₹ already in positions
    deployed_pct: float   # deployed / capital * 100
    source:      str      # "kite" or "config"


def get_effective_capital(config: dict) -> CapitalInfo:
    """
    Returns CapitalInfo. Never crashes — degrades to config-based view
    when Kite is unavailable.
    """
    config_capital = float(config.get("account_capital", 500_000) or 500_000)

    try:
        from integrations.zerodha import get_client_or_none
        kc = get_client_or_none()
    except ImportError:
        kc = None

    if kc is None:
        return CapitalInfo(
            capital=config_capital,
            available=config_capital,
            deployed=0.0,
            deployed_pct=0.0,
            source="config",
        )

    m = kc.margins_equity()
    live_balance = float(m.get("live_balance", 0) or 0)
    available    = float(m.get("available_cash", 0) or 0)
    used         = float(m.get("used", 0) or 0)

    # If margins call returned all zeros, fall back to config
    if live_balance <= 0:
        return CapitalInfo(
            capital=config_capital,
            available=config_capital,
            deployed=0.0,
            deployed_pct=0.0,
            source="config",
        )

    deployed_pct = (used / live_balance * 100) if live_balance else 0.0
    return CapitalInfo(
        capital=round(live_balance, 0),
        available=round(available, 0),
        deployed=round(used, 0),
        deployed_pct=round(deployed_pct, 1),
        source="kite",
    )
