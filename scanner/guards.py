"""
scanner/guards.py — Pre-Trade Safety Guards
============================================
Each guard answers: "Should this trade plan be allowed RIGHT NOW?"

Guards run at two surfaces:
  1. Cockpit display     — `--today` shows ✗ + reason on blocked rows.
  2. Trade recording     — `--record` refuses to log a blocked trade
                           unless `--force` is supplied.

All guards return a `GuardResult(allowed: bool, reason: str)`.
A plan is allowed only if EVERY guard passes.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional


@dataclass
class GuardResult:
    allowed: bool
    reason:  str          # short human-readable explanation
    code:    str = ""     # machine code: DRIFT / GAP / STALE / SECTOR / HEAT


# ─────────────────────────────────────────────────────────────────────────────
# 1. ENTRY DRIFT — block if live price already above planned entry
# ─────────────────────────────────────────────────────────────────────────────
def check_entry_drift(plan: dict, current_price: float,
                       max_drift_pct: float) -> GuardResult:
    entry = float(plan.get("entry_price", 0) or 0)
    if entry <= 0 or current_price <= 0:
        return GuardResult(True, "")
    drift_pct = (current_price - entry) / entry * 100
    if drift_pct > max_drift_pct:
        return GuardResult(
            False,
            f"DRIFT {drift_pct:+.1f}% above entry (limit {max_drift_pct:.1f}%) — chasing",
            "DRIFT",
        )
    return GuardResult(True, "")


# ─────────────────────────────────────────────────────────────────────────────
# 2. GAP-UP — block if today opened materially above entry
# ─────────────────────────────────────────────────────────────────────────────
def check_gap_up(plan: dict, today_open: float,
                  max_gap_pct: float) -> GuardResult:
    entry = float(plan.get("entry_price", 0) or 0)
    if entry <= 0 or today_open <= 0:
        return GuardResult(True, "")
    gap_pct = (today_open - entry) / entry * 100
    if gap_pct > max_gap_pct:
        return GuardResult(
            False,
            f"GAP-UP {gap_pct:+.1f}% above entry (limit {max_gap_pct:.1f}%) — skip",
            "GAP",
        )
    return GuardResult(True, "")


# ─────────────────────────────────────────────────────────────────────────────
# 3. STALE SETUP — block if setup was first seen more than N days ago
# ─────────────────────────────────────────────────────────────────────────────
def check_setup_age(first_seen: Optional[str], max_age_days: int,
                     today: Optional[date] = None) -> GuardResult:
    if not first_seen:
        return GuardResult(True, "")
    if today is None:
        today = date.today()
    try:
        seen = datetime.strptime(str(first_seen)[:10], "%Y-%m-%d").date()
    except ValueError:
        return GuardResult(True, "")
    age = (today - seen).days
    if age > max_age_days:
        return GuardResult(
            False,
            f"STALE — setup first seen {age}d ago (limit {max_age_days}d)",
            "STALE",
        )
    return GuardResult(True, "")


# ─────────────────────────────────────────────────────────────────────────────
# 4. SECTOR CONCENTRATION — block if sector already at cap
# ─────────────────────────────────────────────────────────────────────────────
def check_sector_cap(plan: dict, open_positions: list[dict],
                      sector_map: dict, max_per_sector: int) -> GuardResult:
    ticker = str(plan.get("ticker", ""))
    sector = sector_map.get(ticker, "OTHER")
    if sector == "OTHER":
        # Don't block unmapped sectors
        return GuardResult(True, "")
    count = sum(
        1 for p in open_positions
        if sector_map.get(str(p.get("ticker", "")), "OTHER") == sector
    )
    if count >= max_per_sector:
        return GuardResult(
            False,
            f"SECTOR FULL — {sector} already has {count} open (cap {max_per_sector})",
            "SECTOR",
        )
    return GuardResult(True, "")


# ─────────────────────────────────────────────────────────────────────────────
# 5. PORTFOLIO HEAT — block if adding this trade would exceed total R risk
# ─────────────────────────────────────────────────────────────────────────────
def check_portfolio_heat(plan: dict, open_positions: list[dict],
                          account_capital: float,
                          max_heat_pct: float) -> GuardResult:
    """
    "Heat" = sum of (max_loss_inr) across all open trades, expressed
    as % of account capital. Adding this trade's max_loss is what we check.

    open_positions entries should each have a `risk_per_share` and
    `quantity` (from scanner.positions.load_active_positions).
    """
    if account_capital <= 0:
        return GuardResult(True, "")

    existing_risk = 0.0
    for p in open_positions:
        rps = float(p.get("risk_per_share", 0) or 0)
        qty = float(p.get("quantity", 0) or 0)
        existing_risk += rps * qty

    new_risk = float(plan.get("max_loss_inr", 0) or 0)
    total_risk = existing_risk + new_risk
    heat_pct = total_risk / account_capital * 100
    limit_pct = max_heat_pct * 100 if max_heat_pct < 1 else max_heat_pct

    if heat_pct > limit_pct:
        return GuardResult(
            False,
            f"HEAT {heat_pct:.1f}% would exceed cap {limit_pct:.1f}% "
            f"(existing ₹{existing_risk:,.0f} + new ₹{new_risk:,.0f})",
            "HEAT",
        )
    return GuardResult(True, "")


# ─────────────────────────────────────────────────────────────────────────────
# 6. MAX OPEN POSITIONS — block if at hard cap
# ─────────────────────────────────────────────────────────────────────────────
def check_max_positions(open_positions: list[dict],
                         max_positions: int) -> GuardResult:
    n = len(open_positions)
    if n >= max_positions:
        return GuardResult(
            False,
            f"PORTFOLIO FULL — {n} open (cap {max_positions})",
            "FULL",
        )
    return GuardResult(True, "")


# ─────────────────────────────────────────────────────────────────────────────
# 6b. DAILY-LOSS CIRCUIT BREAKER — block new trades if today's realised
#     P&L breaches the configured drawdown limit.
# ─────────────────────────────────────────────────────────────────────────────
def check_daily_loss_circuit(journal_dir: str,
                              account_capital: float,
                              breaker_pct: float) -> GuardResult:
    """
    Reads trade log, computes today's realised P&L on CLOSED trades,
    and blocks new entries if it has fallen below -breaker_pct * capital.
    """
    if account_capital <= 0 or breaker_pct <= 0:
        return GuardResult(True, "")
    try:
        from analytics.outcome_tracker import load_trade_log
        import pandas as _pd
        df = load_trade_log(journal_dir=journal_dir)
        if df.empty:
            return GuardResult(True, "")
        today = date.today()
        df["exit_date"] = _pd.to_datetime(df.get("exit_date"), errors="coerce")
        closed_today = df[
            (df["status"].astype(str).str.upper() == "CLOSED") &
            (df["exit_date"].dt.date == today)
        ]
        realised = 0.0
        for _, r in closed_today.iterrows():
            entry = float(r.get("entry_price", 0) or 0)
            exitp = float(r.get("exit_price",  0) or 0)
            qty   = float(r.get("quantity",    0) or 0)
            realised += (exitp - entry) * qty
    except Exception:
        return GuardResult(True, "")

    breaker_inr = account_capital * breaker_pct
    if realised <= -breaker_inr:
        return GuardResult(
            False,
            f"DAILY-LOSS CIRCUIT — realised ₹{realised:+,.0f} "
            f"≤ -₹{breaker_inr:,.0f}. No new trades today.",
            "CIRCUIT",
        )
    return GuardResult(True, "")


# ─────────────────────────────────────────────────────────────────────────────
# AGGREGATE — run all guards, return list of failures (empty = allowed)
# ─────────────────────────────────────────────────────────────────────────────
def run_all_guards(plan: dict, context: dict) -> list[GuardResult]:
    """
    context = {
        "current_price":   float,
        "today_open":      float,
        "first_seen":      "YYYY-MM-DD" (scan_date from scan_log),
        "open_positions":  [...] from load_active_positions,
        "config":          CONFIG dict,
    }
    Returns the list of FAILED guards. Empty list => trade is allowed.
    """
    cfg = context.get("config", {})
    failures: list[GuardResult] = []

    cp = context.get("current_price", 0)
    if cp:
        r = check_entry_drift(plan, cp, cfg.get("max_entry_drift_pct", 1.5))
        if not r.allowed: failures.append(r)

    to = context.get("today_open", 0)
    if to:
        r = check_gap_up(plan, to, cfg.get("max_gap_up_pct", 2.5))
        if not r.allowed: failures.append(r)

    fs = context.get("first_seen")
    if fs:
        r = check_setup_age(fs, cfg.get("setup_expiry_days", 10))
        if not r.allowed: failures.append(r)

    op = context.get("open_positions", []) or []
    smap = cfg.get("sector_map", {})
    r = check_sector_cap(plan, op, smap, cfg.get("max_sector_positions", 2))
    if not r.allowed: failures.append(r)

    r = check_portfolio_heat(plan, op,
                              cfg.get("account_capital", 0),
                              cfg.get("max_portfolio_heat", 0.05))
    if not r.allowed: failures.append(r)

    r = check_max_positions(op, cfg.get("max_positions", 6))
    if not r.allowed: failures.append(r)

    jdir = context.get("journal_dir", "journal")
    r = check_daily_loss_circuit(
        journal_dir=jdir,
        account_capital=float(cfg.get("account_capital", 0) or 0),
        breaker_pct=float(cfg.get("daily_loss_breaker_pct", 0) or 0),
    )
    if not r.allowed: failures.append(r)

    return failures


# ─────────────────────────────────────────────────────────────────────────────
# 7. REGIME GATE — block / warn new entries based on NIFTY market state
# ─────────────────────────────────────────────────────────────────────────────
def check_regime_gate(regime: str, config: dict) -> GuardResult:
    """
    Returns:
      allowed=True, code="ALLOW"   — BULL or unrecognised regime
      allowed=True, code="WARN"    — NEUTRAL: proceed but show warning
      allowed=False, code="BLOCK"  — BEAR: hard block, explicit override needed

    Behaviour is driven by config["regime_gate"] so you can loosen
    the gate without touching code.
    """
    gate_cfg = config.get("regime_gate", {})
    action   = gate_cfg.get(regime, "allow").lower()

    if action == "block":
        return GuardResult(
            False,
            f"BEAR REGIME — NIFTY below key SMAs. "
            f"New longs carry high index-level risk.",
            "BLOCK",
        )
    if action == "warn":
        return GuardResult(
            True,
            f"NEUTRAL REGIME — mixed market conditions. "
            f"Reduce size or skip marginal setups.",
            "WARN",
        )
    # allow
    return GuardResult(True, "", "ALLOW")
