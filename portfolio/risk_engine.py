"""
portfolio/risk_engine.py — Fixed-Fractional Risk Budgeting & Position Sizing
============================================================================
The single source of truth for "how many shares may I buy?" against REAL,
live equity. Pure, typed, deterministic — no broker calls, no scoring, no I/O.
The cockpit/order-card layers feed it equity + cash + entry + stop and render
its verdict.

Model: Fixed-Fractional.
    risk_amount = equity × risk_per_trade%          (default 1%)
    qty         = floor(risk_amount / (entry − stop))   (per-share risk)

Guardrails (a trade is DISQUALIFIED, with a reason, if any fail):
  G-A  Invalid stop      — stop ≥ entry (or non-positive prices): no risk basis.
  G-B  Sub-share risk    — even 1 share risks > risk_amount (i.e. risk/share is
                            larger than the whole budget): qty would be 0.
  G-C  Concentration     — a SINGLE share's price exceeds `max_share_pct` of
                            equity (default 15%): the name is too heavy to size
                            cleanly at this account size.
  G-D  Position cap       — position value exceeds `max_position_pct` of equity
                            (default 25%): trim qty to the cap.
  G-E  Cash availability — required cash exceeds `available_cash`: trim qty to
                            what cash allows; if that drops below 1 share,
                            DISQUALIFY (cannot fund even one share).

Trimming order: start from the risk-derived qty, then apply the position cap,
then the cash cap (both can only REDUCE qty). The realized risk after trimming
is always ≤ the risk budget, never above it.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

_log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RiskConfig:
    """Account-level risk policy. Defaults match config/deployment.yaml + the
    user's autopsy (1% risk, 15% single-share concentration ceiling)."""
    risk_per_trade_pct: float = 1.0     # % of equity risked if the stop hits
    max_share_pct:      float = 15.0    # G-C: one share may not exceed this % of equity
    max_position_pct:   float = 25.0    # G-D: a position may not exceed this % of equity
    min_shares:         int   = 1       # below this → not a trade
    lot_size:           int   = 1       # NSE equity delivery = 1; override for F&O

    def risk_amount(self, equity: float) -> float:
        return max(0.0, equity) * self.risk_per_trade_pct / 100.0


# ─────────────────────────────────────────────────────────────────────────────
# RESULT
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class SizingResult:
    symbol:          str
    approved:        bool
    quantity:        int
    entry:           float
    stop:            float
    risk_per_share:  float
    risk_budget:     float            # the 1%-of-equity ₹ budget
    risk_amount:     float            # ACTUAL ₹ at risk = qty × risk_per_share
    position_value:  float            # qty × entry
    position_pct:    float            # position_value / equity × 100
    status:          str              # APPROVED | DISQUALIFIED | TRIMMED
    reason:          str
    flags:           list[str] = field(default_factory=list)   # which guardrails bound/blocked

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "approved": self.approved, "status": self.status,
            "quantity": self.quantity, "entry": self.entry, "stop": self.stop,
            "risk_per_share": round(self.risk_per_share, 2),
            "risk_budget": round(self.risk_budget, 2),
            "risk_amount": round(self.risk_amount, 2),
            "position_value": round(self.position_value, 2),
            "position_pct": round(self.position_pct, 2),
            "reason": self.reason, "flags": self.flags,
        }


def _disqualified(symbol: str, entry: float, stop: float, budget: float,
                  reason: str, flags: list[str]) -> SizingResult:
    return SizingResult(
        symbol=symbol, approved=False, quantity=0, entry=entry, stop=stop,
        risk_per_share=max(0.0, entry - stop), risk_budget=budget, risk_amount=0.0,
        position_value=0.0, position_pct=0.0, status="DISQUALIFIED",
        reason=reason, flags=flags)


# ─────────────────────────────────────────────────────────────────────────────
# SIZER
# ─────────────────────────────────────────────────────────────────────────────
class FixedFractionalSizer:
    """Fixed-fractional position sizing with hard guardrails."""

    def __init__(self, config: Optional[RiskConfig] = None) -> None:
        self.cfg = config or RiskConfig()

    # ── single trade ─────────────────────────────────────────────────────────
    def size(self, *, symbol: str, equity: float, available_cash: float,
             entry: float, stop: float,
             risk_pct: Optional[float] = None) -> SizingResult:
        """Size one trade. `risk_pct` overrides the config (e.g. adaptive-risk
        already scaled it down to 0.5% in a MIXED edge regime)."""
        cfg = self.cfg
        rpct = cfg.risk_per_trade_pct if risk_pct is None else float(risk_pct)
        budget = max(0.0, equity) * rpct / 100.0

        # G-pre: sane inputs
        if equity <= 0:
            return _disqualified(symbol, entry, stop, budget,
                                 "no equity — fund the account before sizing", ["NO_EQUITY"])
        if entry <= 0:
            return _disqualified(symbol, entry, stop, budget,
                                 "invalid entry price", ["BAD_PRICE"])

        # G-A: stop must sit below entry to define risk
        risk_ps = entry - stop
        if stop <= 0 or risk_ps <= 0:
            return _disqualified(symbol, entry, stop, budget,
                                 f"invalid stop (entry ₹{entry:,.2f} ≤ stop ₹{stop:,.2f}) "
                                 f"— no risk basis", ["INVALID_STOP"])

        # G-C: single-share concentration ceiling (the autopsy's POWERINDIA case)
        share_pct = entry / equity * 100.0
        if share_pct > cfg.max_share_pct:
            return _disqualified(
                symbol, entry, stop, budget,
                f"1 share (₹{entry:,.0f}) = {share_pct:.1f}% of equity "
                f"> {cfg.max_share_pct:.0f}% cap — too heavy for this account size",
                ["CONCENTRATION"])

        flags: list[str] = []

        # Core fixed-fractional quantity
        qty = math.floor(budget / risk_ps)

        # G-B: budget cannot fund even one share's risk
        if qty < cfg.min_shares:
            return _disqualified(
                symbol, entry, stop, budget,
                f"risk/share ₹{risk_ps:,.2f} > risk budget ₹{budget:,.0f} "
                f"(1% of equity) — stop too wide to size ≥1 share",
                ["SUBSHARE_RISK"])

        # G-D: position-value cap (≤ max_position_pct of equity)
        max_pos_value = equity * cfg.max_position_pct / 100.0
        qty_by_pos = math.floor(max_pos_value / entry)
        if qty_by_pos < qty:
            qty = qty_by_pos
            flags.append("POSITION_CAP")

        # G-E: cash availability — can only reduce
        qty_by_cash = math.floor(max(0.0, available_cash) / entry)
        if qty_by_cash < qty:
            qty = qty_by_cash
            flags.append("CASH_CAP")

        # Lot rounding (1 for cash equity)
        if cfg.lot_size > 1:
            qty = (qty // cfg.lot_size) * cfg.lot_size

        if qty < cfg.min_shares:
            return _disqualified(
                symbol, entry, stop, budget,
                f"insufficient cash — ₹{available_cash:,.0f} cannot fund 1 share "
                f"at ₹{entry:,.0f}", ["CASH_CAP", "UNFUNDED"])

        pos_value = qty * entry
        risk_amt  = qty * risk_ps
        status    = "TRIMMED" if flags else "APPROVED"
        reason = (f"{qty} sh × ₹{entry:,.2f} = ₹{pos_value:,.0f} "
                  f"({pos_value/equity*100:.1f}% of equity) · "
                  f"risk ₹{risk_amt:,.0f} ({risk_amt/equity*100:.2f}% of equity)")
        if flags:
            reason += f" · trimmed by {', '.join(flags)}"

        _log.debug("sized %s: qty=%d value=%.0f risk=%.0f flags=%s",
                   symbol, qty, pos_value, risk_amt, flags)
        return SizingResult(
            symbol=symbol, approved=True, quantity=qty, entry=entry, stop=stop,
            risk_per_share=risk_ps, risk_budget=budget, risk_amount=risk_amt,
            position_value=pos_value, position_pct=round(pos_value / equity * 100, 2),
            status=status, reason=reason, flags=flags)

    # ── a basket under a shared cash + book-heat budget ───────────────────────
    def size_book(self, candidates: list[dict], *, equity: float,
                  available_cash: float, max_book_heat_pct: float = 6.0,
                  risk_pct: Optional[float] = None) -> list[SizingResult]:
        """Greedy, conviction-ordered fill of a list of candidates under a shared
        cash pool AND a total book-heat ceiling. Each candidate dict needs at
        least: symbol, entry, stop (optional: risk_pct override per name).

        Candidates are expected pre-sorted by conviction (the caller's ranking is
        respected — this engine never re-ranks). Returns one SizingResult per
        candidate; later names that exhaust cash/heat come back DISQUALIFIED."""
        cash_left = max(0.0, available_cash)
        heat_budget = equity * max_book_heat_pct / 100.0
        heat_used = 0.0
        out: list[SizingResult] = []

        for c in candidates:
            res = self.size(symbol=c["symbol"], equity=equity,
                            available_cash=cash_left, entry=float(c["entry"]),
                            stop=float(c["stop"]),
                            risk_pct=c.get("risk_pct", risk_pct))
            if res.approved:
                # Enforce the shared book-heat ceiling (trim or block this name)
                if heat_used + res.risk_amount > heat_budget:
                    room = heat_budget - heat_used
                    if room <= 0:
                        res = _disqualified(
                            c["symbol"], res.entry, res.stop, res.risk_budget,
                            f"book heat ceiling {max_book_heat_pct:.1f}% reached "
                            f"— no risk room left", ["BOOK_HEAT"])
                    else:
                        allowed_qty = math.floor(room / res.risk_per_share)
                        if allowed_qty < self.cfg.min_shares:
                            res = _disqualified(
                                c["symbol"], res.entry, res.stop, res.risk_budget,
                                f"book heat ceiling {max_book_heat_pct:.1f}% reached "
                                f"— remaining room < 1 share of risk", ["BOOK_HEAT"])
                        else:
                            res = self.size(symbol=c["symbol"], equity=equity,
                                            available_cash=cash_left,
                                            entry=res.entry, stop=res.stop,
                                            risk_pct=c.get("risk_pct", risk_pct))
                            # re-cap qty to heat room
                            if res.approved and res.quantity > allowed_qty:
                                res = _resize_to_qty(res, allowed_qty, equity)
                                res.flags.append("BOOK_HEAT")
                                res.status = "TRIMMED"
                if res.approved:
                    cash_left -= res.position_value
                    heat_used += res.risk_amount
            out.append(res)
        return out


def _resize_to_qty(res: SizingResult, qty: int, equity: float) -> SizingResult:
    """Recompute a result's derived ₹ fields after an external qty trim."""
    pos_value = qty * res.entry
    risk_amt  = qty * res.risk_per_share
    res.quantity = qty
    res.position_value = pos_value
    res.position_pct = round(pos_value / equity * 100, 2) if equity else 0.0
    res.risk_amount = risk_amt
    res.reason = (f"{qty} sh × ₹{res.entry:,.2f} = ₹{pos_value:,.0f} · "
                  f"risk ₹{risk_amt:,.0f} ({risk_amt/equity*100:.2f}% of equity)")
    return res


# ─────────────────────────────────────────────────────────────────────────────
# CONVENIENCE
# ─────────────────────────────────────────────────────────────────────────────
def size_from_config(*, symbol: str, equity: float, available_cash: float,
                     entry: float, stop: float,
                     risk_pct: Optional[float] = None) -> SizingResult:
    """One-shot sizing using config/deployment.yaml's risk policy."""
    try:
        from portfolio.portfolio_state import load_deployment_config
        dc = load_deployment_config()
        cfg = RiskConfig(risk_per_trade_pct=float(dc.get("risk_per_trade_pct", 1.0)))
    except Exception:
        cfg = RiskConfig()
    return FixedFractionalSizer(cfg).size(
        symbol=symbol, equity=equity, available_cash=available_cash,
        entry=entry, stop=stop, risk_pct=risk_pct)
