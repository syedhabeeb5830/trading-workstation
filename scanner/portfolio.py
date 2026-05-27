"""
scanner/portfolio.py  —  Portfolio Intelligence Engine
=======================================================
Real-money execution requires portfolio-level awareness.
Individual trade quality means nothing if you're running:
  - 6 correlated IT longs
  - 80% capital in one sector
  - Total open risk of 8% when limit is 5%

This module answers:
  "Given my open positions, what can I SAFELY add?"

Functions:
  load_portfolio_state()   — reads open trades, computes heat
  check_sector_crowding()  — blocks same-sector overexposure
  get_open_risk_inr()      — total ₹ at risk across all open trades
  filter_by_portfolio()    — removes setups that would breach limits
  print_portfolio_summary() — cockpit display
"""

import os
import sys
import pandas as pd
from pathlib import Path


def load_portfolio_state(journal_dir: str = "journal") -> dict:
    """
    Loads open trades from trades.csv and computes portfolio metrics.

    Returns:
        {
          "open_trades":         list of open trade dicts
          "open_risk_inr":       total ₹ at risk (sum of max_loss_inr)
          "open_risk_pct":       open risk / account_capital
          "sector_counts":       {sector: count}
          "position_count":      int
          "heat_used_pct":       portfolio heat used %
          "heat_remaining_pct":  remaining heat %
        }
    """
    try:
        from analytics.outcome_tracker import load_trade_log
        from config.config import CONFIG
    except ModuleNotFoundError:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from analytics.outcome_tracker import load_trade_log
        from config.config import CONFIG

    df = load_trade_log(journal_dir)
    capital = CONFIG["account_capital"]

    empty_state = {
        "open_trades":        [],
        "open_risk_inr":      0.0,
        "open_risk_pct":      0.0,
        "sector_counts":      {},
        "position_count":     0,
        "heat_used_pct":      0.0,
        "heat_remaining_pct": CONFIG["max_portfolio_heat"] * 100,
    }

    if df.empty:
        return empty_state

    open_mask = (
        (df["resolved"].astype(str).str.upper() != "TRUE") &
        (df["exit_reason"].astype(str).isin(["OPEN", "", "nan"]))
    )
    open_df = df[open_mask].copy()

    if open_df.empty:
        return empty_state

    # Compute risk per open trade
    open_trades = []
    total_risk  = 0.0
    sector_counts = {}

    for _, row in open_df.iterrows():
        ticker   = str(row.get("ticker", ""))
        max_loss = float(row.get("max_loss_inr", 0) or 0)
        sector   = CONFIG.get("sector_map", {}).get(ticker, "OTHER")

        total_risk += max_loss
        sector_counts[sector] = sector_counts.get(sector, 0) + 1

        open_trades.append({
            "ticker":     ticker,
            "sector":     sector,
            "max_loss":   max_loss,
            "entry_date": str(row.get("entry_date", ""))[:10],
            "grade":      str(row.get("grade", "")),
        })

    heat_used      = total_risk / capital
    heat_remaining = max(0.0, CONFIG["max_portfolio_heat"] - heat_used)

    return {
        "open_trades":        open_trades,
        "open_risk_inr":      round(total_risk, 2),
        "open_risk_pct":      round(heat_used * 100, 2),
        "sector_counts":      sector_counts,
        "position_count":     len(open_trades),
        "heat_used_pct":      round(heat_used * 100, 2),
        "heat_remaining_pct": round(heat_remaining * 100, 2),
    }


def check_sector_crowding(ticker: str, sector_counts: dict,
                           config: dict) -> tuple[bool, str]:
    """
    Returns (is_crowded: bool, reason: str).
    Crowded = already have max_sector_positions in this sector.
    """
    sector  = config.get("sector_map", {}).get(ticker, "OTHER")
    current = sector_counts.get(sector, 0)
    max_s   = config["max_sector_positions"]

    if current >= max_s:
        return True, f"Sector limit: {current}/{max_s} {sector} positions already open"
    return False, ""


def filter_by_portfolio(plans: list, portfolio: dict, config: dict) -> list:
    """
    Filters and annotates trade plans based on portfolio state.
    Does NOT remove plans — adds portfolio_ok, portfolio_note fields.
    The trader makes the final call; this just informs.

    Annotations added to each plan:
      portfolio_ok    — True if portfolio constraints are met
      portfolio_note  — human-readable reason if blocked
    """
    for plan in plans:
        ticker = plan["ticker"]
        sector = plan.get("sector", "OTHER")

        # Check 1: max positions
        if portfolio["position_count"] >= config["max_positions"]:
            plan["portfolio_ok"]   = False
            plan["portfolio_note"] = (
                f"Max positions reached ({config['max_positions']})"
            )
            continue

        # Check 2: portfolio heat
        if portfolio["heat_remaining_pct"] < 0.5:
            plan["portfolio_ok"]   = False
            plan["portfolio_note"] = (
                f"Portfolio heat full ({portfolio['heat_used_pct']:.1f}%"
                f" / {config['max_portfolio_heat']*100:.0f}%)"
            )
            continue

        # Check 3: sector crowding
        crowded, reason = check_sector_crowding(
            ticker, portfolio["sector_counts"], config
        )
        if crowded:
            plan["portfolio_ok"]   = False
            plan["portfolio_note"] = reason
            continue

        plan["portfolio_ok"]   = True
        plan["portfolio_note"] = ""

    return plans