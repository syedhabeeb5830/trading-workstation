"""
deploy/order_card.py — Capital-Aware Order Card
================================================
Takes the system's TARGET portfolio (PortfolioSnapshot, in % allocations) and
the CURRENT portfolio (PortfolioState, in real quantities and cash), then
computes exactly what trades are needed and renders a human-readable order card.

Inputs:
  snap     — PortfolioSnapshot  (target allocation from PortfolioConstructor)
  state    — PortfolioState     (actual broker positions + cash)
  capital  — float              (total capital in INR)
  lc_snap  — LifecycleSnapshot  (optional — EXIT/REDUCE/ADD signals for existing holdings)

Output:
  OrderCard  (BUY list + SELL list + HOLD list + cash summary)

Pure read + render: modifies NOTHING in the engines or state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from portfolio.portfolio_engine import PortfolioSnapshot
from portfolio.portfolio_state import PortfolioState, LiveHolding

_DUST_VALUE       = 5_000.0   # skip orders whose value is below this (₹)
_DRIFT_THRESHOLD  = 2.0       # rebalance only when drift exceeds this (%)


# ─────────────────────────────────────────────────────────────────────────────
# DATA TYPES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeInstruction:
    symbol:          str
    ticker:          str
    action:          str            # BUY | SELL | HOLD
    quantity:        int
    limit_price:     float
    stop_price:      Optional[float]
    estimated_value: float          # abs(quantity) × limit_price
    reason:          str
    pnl_pct:         float = 0.0          # for SELL/HOLD lines (display only)
    target1:         Optional[float] = None   # G4: T1 from actionability engine
    target2:         Optional[float] = None   # G4: T2 = T1 + (T1 − entry) × 0.5
    risk_inr:        float = 0.0             # G4: ₹ risk = (entry − stop) × qty
    weeks_held:      Optional[int]  = None   # G5: weeks since entry_date


@dataclass
class OrderCard:
    regime:               str
    source:               str
    capital:              float
    target_deployed_pct:  float    # what the system recommends (from snap.deployed)
    current_deployed_pct: float    # where we actually are right now
    buys:                 list[TradeInstruction]
    sells:                list[TradeInstruction]
    holds:                list[TradeInstruction]
    cash_required:        float    # net cash outflow (buys − sell proceeds), ≥0
    cash_available:       float    # broker available cash
    cash_after:           float    # cash_available + sell proceeds − buy spend
    deployed_after_pct:   float    # (current holdings + buys − sells) / capital × 100
    book_risk_inr:        float    # Σ position risk in ₹ (from snap.positions risk_contribution)
    book_risk_pct:        float
    n_positions_after:    int
    next_review_date:     str
    generated_at:         str
    posture:              str      # paper | small | full


# ─────────────────────────────────────────────────────────────────────────────
# BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_order_card(
    snap:     PortfolioSnapshot,
    state:    PortfolioState,
    capital:  float,
    lc_snap=None,           # Optional[LifecycleSnapshot]
    posture:  str = "paper",
) -> OrderCard:
    """
    Diff target vs current → compute BUY / SELL / HOLD instructions.

    Algorithm
    ---------
    For each target position (from PortfolioSnapshot):
      target_value = allocation% × capital
      target_qty   = floor(target_value / entry_price)

      if not currently held → BUY target_qty
      if currently held:
        lifecycle == EXIT   → SELL all current qty
        lifecycle == REDUCE → SELL down to target_qty
        lifecycle == ADD    → BUY up to target_qty
        lifecycle == ROTATE → SELL (replacement appears as a BUY via its target slot)
        lifecycle == HOLD   → HOLD (rebalance only if drift > threshold)

    For each held position NOT in the target:
      lifecycle == HOLD → HOLD (keep, unusual)
      otherwise         → SELL (not in target + no hold signal = exit)
    """
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")

    by_sym  = {p.symbol: p for p in snap.positions}
    cur_map = state.holdings_by_symbol()
    lc_map  = {r.symbol: r for r in lc_snap.rows} if lc_snap else {}

    buys:  list[TradeInstruction] = []
    sells: list[TradeInstruction] = []
    holds: list[TradeInstruction] = []
    processed: set[str] = set()

    # ── 1. iterate over TARGET positions ────────────────────────────────────
    for sym, tgt in by_sym.items():
        processed.add(sym)
        price = tgt.entry if (tgt.entry and tgt.entry > 0) else 0.0
        tgt_val = tgt.allocation / 100.0 * capital
        tgt_qty = int(tgt_val / price) if price > 0 else 0
        stop    = tgt.stop if (tgt.stop and tgt.stop > 0) else None
        # G4: T1/T2 targets and ₹ risk (reused by all BUY instructions in this slot)
        _tgt_t = getattr(tgt, "target", 0.0) or 0.0
        _t1 = round(_tgt_t, 2) if _tgt_t > price > 0 else None
        _t2 = round(_t1 + (_t1 - price) * 0.5, 2) if _t1 else None
        _risk_ps = max(0.0, price - stop) if stop else 0.0   # per share

        cur = cur_map.get(sym)
        lc  = lc_map.get(sym)

        # ── New position ──────────────────────────────────────────────────
        if cur is None:
            if tgt_qty > 0 and tgt_val >= _DUST_VALUE:
                buys.append(TradeInstruction(
                    symbol=sym, ticker=tgt.ticker, action="BUY",
                    quantity=tgt_qty, limit_price=round(price, 2),
                    stop_price=round(stop, 2) if stop else None,
                    estimated_value=round(tgt_qty * price, 2),
                    reason=f"New — {tgt.allocation:.0f}% target  ≈ ₹{tgt_val:,.0f}  |  "
                           f"{tgt.rs_status}  {tgt.classification}",
                    target1=_t1, target2=_t2,
                    risk_inr=round(_risk_ps * tgt_qty, 2),
                ))
            continue

        # ── Existing position: lifecycle overrides ────────────────────────
        cur_p = cur.current_price if cur.current_price > 0 else cur.avg_price

        if lc and lc.status == "EXIT":
            if cur.quantity > 0:
                sells.append(TradeInstruction(
                    symbol=sym, ticker=tgt.ticker, action="SELL",
                    quantity=cur.quantity, limit_price=round(cur_p, 2),
                    stop_price=None,
                    estimated_value=round(cur.quantity * cur_p, 2),
                    reason=lc.reason,
                    pnl_pct=cur.unrealized_pnl_pct,
                ))

        elif lc and lc.status == "REDUCE":
            reduce_qty = max(0, cur.quantity - tgt_qty)
            if reduce_qty > 0 and reduce_qty * cur_p >= _DUST_VALUE:
                sells.append(TradeInstruction(
                    symbol=sym, ticker=tgt.ticker, action="SELL",
                    quantity=reduce_qty, limit_price=round(cur_p, 2),
                    stop_price=round(stop, 2) if stop else None,
                    estimated_value=round(reduce_qty * cur_p, 2),
                    reason=lc.reason,
                    pnl_pct=cur.unrealized_pnl_pct,
                ))
            else:
                holds.append(_make_hold(sym, tgt.ticker, cur, cur_p, stop,
                                        lc.reason or "REDUCE — already at/below target",
                                        weeks_held=lc.weeks_held if lc else None))

        elif lc and lc.status == "ADD":
            add_qty = max(0, tgt_qty - cur.quantity)
            if add_qty > 0 and add_qty * price >= _DUST_VALUE:
                buys.append(TradeInstruction(
                    symbol=sym, ticker=tgt.ticker, action="BUY",
                    quantity=add_qty, limit_price=round(price, 2),
                    stop_price=round(stop, 2) if stop else None,
                    estimated_value=round(add_qty * price, 2),
                    reason=lc.reason,
                    pnl_pct=cur.unrealized_pnl_pct,
                    target1=_t1, target2=_t2,
                    risk_inr=round(_risk_ps * add_qty, 2),
                ))
            else:
                holds.append(_make_hold(sym, tgt.ticker, cur, cur_p, stop,
                                        "ADD — already at/above target size",
                                        weeks_held=lc.weeks_held if lc else None))

        elif lc and lc.status == "ROTATE":
            # Exit the held name; the replacement will appear as a BUY if it's
            # in the target portfolio (PortfolioConstructor already selected it).
            if cur.quantity > 0:
                sells.append(TradeInstruction(
                    symbol=sym, ticker=tgt.ticker, action="SELL",
                    quantity=cur.quantity, limit_price=round(cur_p, 2),
                    stop_price=None,
                    estimated_value=round(cur.quantity * cur_p, 2),
                    reason=f"ROTATE → {lc.rotate_to}  |  {lc.reason}",
                    pnl_pct=cur.unrealized_pnl_pct,
                ))

        else:
            # HOLD — rebalance only if drift is material
            drift = abs(cur.allocation_pct - tgt.allocation)
            if drift > _DRIFT_THRESHOLD and tgt_qty != cur.quantity:
                delta = tgt_qty - cur.quantity
                if delta > 0 and delta * price >= _DUST_VALUE:
                    buys.append(TradeInstruction(
                        symbol=sym, ticker=tgt.ticker, action="BUY",
                        quantity=delta, limit_price=round(price, 2),
                        stop_price=round(stop, 2) if stop else None,
                        estimated_value=round(delta * price, 2),
                        reason=f"Rebalance — drift {drift:.1f}% above {_DRIFT_THRESHOLD:.0f}% threshold",
                        pnl_pct=cur.unrealized_pnl_pct,
                        target1=_t1, target2=_t2,
                        risk_inr=round(_risk_ps * delta, 2),
                    ))
                elif delta < 0 and abs(delta) * cur_p >= _DUST_VALUE:
                    sells.append(TradeInstruction(
                        symbol=sym, ticker=tgt.ticker, action="SELL",
                        quantity=abs(delta), limit_price=round(cur_p, 2),
                        stop_price=None,
                        estimated_value=round(abs(delta) * cur_p, 2),
                        reason=f"Rebalance — drift {drift:.1f}% above {_DRIFT_THRESHOLD:.0f}% threshold",
                        pnl_pct=cur.unrealized_pnl_pct,
                    ))
                else:
                    holds.append(_make_hold(sym, tgt.ticker, cur, cur_p, stop,
                                            lc.reason if lc else "HOLD — within drift tolerance",
                                            weeks_held=lc.weeks_held if lc else None))
            else:
                holds.append(_make_hold(sym, tgt.ticker, cur, cur_p, stop,
                                        lc.reason if lc else "HOLD — RS strong, trend intact",
                                        weeks_held=lc.weeks_held if lc else None))

    # ── 2. Held positions NOT in target ──────────────────────────────────────
    for sym, cur in cur_map.items():
        if sym in processed:
            continue
        lc    = lc_map.get(sym)
        cur_p = cur.current_price if cur.current_price > 0 else cur.avg_price

        if lc and lc.status == "HOLD":
            holds.append(_make_hold(sym, cur.ticker, cur, cur_p, cur.stop_price,
                                    "Not in new target — lifecycle says HOLD, keeping",
                                    weeks_held=lc.weeks_held if lc else None))
        elif cur.quantity > 0:
            sells.append(TradeInstruction(
                symbol=sym, ticker=cur.ticker, action="SELL",
                quantity=cur.quantity, limit_price=round(cur_p, 2),
                stop_price=None,
                estimated_value=round(cur.quantity * cur_p, 2),
                reason=lc.reason if lc else "Not in target portfolio",
                pnl_pct=cur.unrealized_pnl_pct,
            ))

    # ── 3. Cash flows and portfolio-after snapshot ────────────────────────────
    buy_spend     = sum(b.estimated_value for b in buys)
    sell_proceeds = sum(s.estimated_value for s in sells)
    cash_required = max(0.0, buy_spend - sell_proceeds)
    cash_after    = state.available_cash + sell_proceeds - buy_spend

    # Value of positions we'll hold after execution
    hold_value = sum(h.estimated_value for h in holds)
    remaining  = state.holdings_value - sum(s.estimated_value for s in sells)
    deployed_after_value = remaining + buy_spend
    deployed_after_pct   = round(deployed_after_value / capital * 100, 2) if capital else 0.0

    n_after = len(holds) + len(buys)

    # Book risk from target snapshot (risk_contribution is already % of capital per position)
    book_risk_inr = sum(p.risk_contribution * capital / 100.0 for p in snap.positions)
    book_risk_pct = round(book_risk_inr / capital * 100, 2) if capital else 0.0

    return OrderCard(
        regime=snap.regime, source=state.source, capital=capital,
        target_deployed_pct=snap.deployed,
        current_deployed_pct=state.deployed_pct,
        buys=buys, sells=sells, holds=holds,
        cash_required=round(max(0.0, cash_required), 2),
        cash_available=round(state.available_cash, 2),
        cash_after=round(cash_after, 2),
        deployed_after_pct=deployed_after_pct,
        book_risk_inr=round(book_risk_inr, 2),
        book_risk_pct=book_risk_pct,
        n_positions_after=n_after,
        next_review_date=_next_friday(),
        generated_at=now,
        posture=posture,
    )


# ─────────────────────────────────────────────────────────────────────────────
# RENDERER
# ─────────────────────────────────────────────────────────────────────────────

def render_order_card(card: OrderCard) -> None:
    """Print the order card to stdout with ANSI colour."""
    G, R, Y, B, D, C, RST = (
        "\033[92m", "\033[91m", "\033[93m",
        "\033[1m",  "\033[2m",  "\033[96m", "\033[0m",
    )
    W = 66

    def inr(v: float) -> str:
        return f"₹{v:,.0f}"

    def pnl_str(v: float) -> str:
        col = G if v >= 0 else R
        return f"{col}{v:+.1f}%{RST}"

    bar  = "═" * W
    thin = "─" * W

    print(f"\n  {B}{bar}{RST}")
    print(f"  {B}  ORDER CARD  ·  {card.generated_at[:10]}  ·  Capital: {inr(card.capital)}{RST}")
    print(f"  {B}{bar}{RST}")
    print(f"  {D}Regime: {card.regime}  ·  Posture: {card.posture.upper()}  ·  "
          f"Source: {card.source}{RST}")
    print(f"  {D}Target deployed: {card.target_deployed_pct:.0f}%  ·  "
          f"Currently deployed: {card.current_deployed_pct:.1f}%{RST}")

    # ── BUY ─────────────────────────────────────────────────────────────────
    if card.buys:
        print(f"\n  {G}{thin}{RST}")
        print(f"  {B}{G}  BUY{RST}")
        print(f"  {G}{thin}{RST}")
        for b in card.buys:
            stop_tag = f"  stop {inr(b.stop_price)}" if b.stop_price else ""
            print(f"  {G}{B}{b.symbol:<12s}{RST}  "
                  f"{b.quantity:>5d} sh  @  {inr(b.limit_price):>10s}   "
                  f"≈  {G}{inr(b.estimated_value)}{RST}"
                  f"{D}{stop_tag}{RST}")
            if b.target1:
                t2_tag = f"  ·  T2 {inr(b.target2)}" if b.target2 else ""
                risk_tag = f"  ·  Risk {inr(b.risk_inr)}" if b.risk_inr else ""
                print(f"       {D}T1 {inr(b.target1)}{t2_tag}{risk_tag}{RST}")
            print(f"       {D}{b.reason}{RST}")
    else:
        print(f"\n  {D}No BUY orders this review — cash stays.{RST}")

    # ── SELL ─────────────────────────────────────────────────────────────────
    if card.sells:
        print(f"\n  {R}{thin}{RST}")
        print(f"  {B}{R}  SELL / EXIT{RST}")
        print(f"  {R}{thin}{RST}")
        for s in card.sells:
            print(f"  {R}{B}{s.symbol:<12s}{RST}  "
                  f"{s.quantity:>5d} sh  @  {inr(s.limit_price):>10s}   "
                  f"≈  {R}{inr(s.estimated_value)}{RST}  "
                  f"{D}P&L {pnl_str(s.pnl_pct)}{RST}")
            print(f"       {D}{s.reason}{RST}")

    # ── HOLD ─────────────────────────────────────────────────────────────────
    if card.holds:
        print(f"\n  {D}{thin}{RST}")
        print(f"  {B}  HOLD (no action needed){RST}")
        print(f"  {D}{thin}{RST}")
        for h in card.holds:
            stop_tag = f"  stop {inr(h.stop_price)}" if h.stop_price else ""
            wh_tag = f"  held {h.weeks_held}w" if h.weeks_held is not None else ""
            print(f"  {D}{h.symbol:<12s}  "
                  f"{h.quantity:>5d} sh  ·  {inr(h.estimated_value):>10s}  "
                  f"·  P&L {pnl_str(h.pnl_pct)}{stop_tag}{wh_tag}{RST}")
    elif not card.buys and not card.sells:
        print(f"\n  {D}No open positions. Book is empty.{RST}")

    # ── CASH ─────────────────────────────────────────────────────────────────
    print(f"\n  {C}{thin}{RST}")
    print(f"  {B}{C}  CASH{RST}")
    print(f"  {C}{thin}{RST}")
    avail_col = G if card.cash_available >= card.cash_required else R
    after_col = G if card.cash_after >= 0 else R
    print(f"  {D}Required today:  {RST}{inr(card.cash_required):>14s}")
    print(f"  {D}Available:       {RST}{avail_col}{inr(card.cash_available):>14s}{RST}")
    print(f"  {D}Cash after:      {RST}{after_col}{inr(card.cash_after):>14s}{RST}")

    if card.cash_after < 0:
        shortfall = abs(card.cash_after)
        print(f"\n  {R}  ⚠  Cash short by {inr(shortfall)} — reduce BUY size or skip "
              f"lowest-conviction name.{RST}")

    # ── Portfolio after ──────────────────────────────────────────────────────
    deployed_inr = card.capital * card.deployed_after_pct / 100.0
    cash_inr     = card.capital - deployed_inr
    print(f"\n  {B}{thin}{RST}")
    print(f"  {B}  PORTFOLIO AFTER EXECUTION{RST}")
    print(f"  {B}{thin}{RST}")
    print(f"  {D}Deployed:    {RST}{G}{inr(deployed_inr)} ({card.deployed_after_pct:.1f}%){RST}")
    print(f"  {D}Cash:        {RST}{inr(cash_inr)} ({100 - card.deployed_after_pct:.1f}%)")
    print(f"  {D}Positions:   {RST}{card.n_positions_after}")
    risk_col = G if card.book_risk_pct <= 5 else Y
    print(f"  {D}Book risk:   {RST}{risk_col}{inr(card.book_risk_inr)} "
          f"({card.book_risk_pct:.2f}% of capital){RST}")

    # ── Footer ───────────────────────────────────────────────────────────────
    posture_warn = ""
    if card.posture == "paper":
        posture_warn = f"  {Y}[PAPER MODE — do not place real orders]{RST}"

    print(f"\n  {B}{bar}{RST}")
    if posture_warn:
        print(f"{posture_warn}")
    print(f"  {B}  IGNORE ALL OTHER STOCKS{RST}")
    print(f"  {D}  Next review:      {card.next_review_date} (EOD){RST}")
    print(f"  {D}  Max positions:    {card.n_positions_after}{RST}")
    print(f"  {D}  Holding horizon:  4–12 weeks per position{RST}")
    print(f"  {D}  Partial exit:     Book 50% at T1 — trail stop to breakeven{RST}")
    print(f"  {B}{bar}{RST}\n")


# ─────────────────────────────────────────────────────────────────────────────
# RENDER SYNC SUMMARY (used by --sync-portfolio output)
# ─────────────────────────────────────────────────────────────────────────────

def render_state_summary(state: PortfolioState) -> None:
    """Print a compact summary of the synced portfolio state."""
    G, R, Y, B, D, RST = ("\033[92m", "\033[91m", "\033[93m",
                            "\033[1m",  "\033[2m",  "\033[0m")

    def inr(v: float) -> str:
        return f"₹{v:,.0f}"

    print(f"\n  {B}{'═'*66}{RST}")
    print(f"  {B}  PORTFOLIO STATE  ·  {state.as_of[:10]}{RST}")
    print(f"  {B}{'═'*66}{RST}")
    print(f"  {D}Source:    {RST}{state.source}")
    print(f"  {D}Capital:   {RST}{inr(state.total_capital)}")
    print(f"  {D}Holdings:  {RST}{G}{inr(state.holdings_value)} ({state.deployed_pct:.1f}%){RST}")
    print(f"  {D}Cash:      {RST}{inr(state.available_cash)} ({state.cash_pct:.1f}%)")
    pnl_col = G if state.unrealized_pnl >= 0 else R
    print(f"  {D}Unrealised P&L: {RST}{pnl_col}{inr(state.unrealized_pnl)}{RST}")
    if getattr(state, "realized_pnl", 0.0):
        rpnl_col = G if state.realized_pnl >= 0 else R
        print(f"  {D}Realised P&L:   {RST}{rpnl_col}{inr(state.realized_pnl)}{RST}  "
              f"{D}(session){RST}")

    if state.holdings:
        print(f"\n  {B}{'SYMBOL':12s}  {'QTY':>5}  {'AVG':>10}  {'LTP':>10}  "
              f"{'P&L%':>7}  {'VALUE':>12}  STOP{RST}")
        for h in state.holdings:
            pnl_col = G if h.unrealized_pnl_pct >= 0 else R
            stop_str = f"₹{h.stop_price:,.0f}" if h.stop_price else "—"
            print(f"  {h.symbol:12s}  {h.quantity:>5d}  ₹{h.avg_price:>9,.2f}  "
                  f"₹{h.current_price:>9,.2f}  "
                  f"{pnl_col}{h.unrealized_pnl_pct:>+6.1f}%{RST}  "
                  f"₹{h.current_value:>11,.0f}  {stop_str}")
    else:
        print(f"\n  {D}No equity holdings found.{RST}")

    if state.open_orders:
        print(f"\n  {B}Open orders ({len(state.open_orders)}){RST}")
        for o in state.open_orders:
            col = G if o.side == "BUY" else R
            print(f"  {col}{o.side:4s}{RST}  {o.symbol:12s}  "
                  f"{o.quantity:>5d} sh  @  ₹{o.price:,.2f}  [{o.status}]")

    print()


# ─────────────────────────────────────────────────────────────────────────────
# PRIVATE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _make_hold(symbol: str, ticker: str, cur: LiveHolding,
               cur_p: float, stop: Optional[float], reason: str,
               weeks_held: Optional[int] = None) -> TradeInstruction:
    return TradeInstruction(
        symbol=symbol, ticker=ticker, action="HOLD",
        quantity=cur.quantity, limit_price=round(cur_p, 2),
        stop_price=round(stop, 2) if stop else cur.stop_price,
        estimated_value=round(cur.quantity * cur_p, 2),
        reason=reason,
        pnl_pct=cur.unrealized_pnl_pct,
        weeks_held=weeks_held,
    )


def _next_friday(from_date: Optional[date] = None) -> str:
    d = from_date or date.today()
    days = (4 - d.weekday()) % 7   # 4 = Friday
    if days == 0:
        days = 7
    return (d + timedelta(days=days)).strftime("%a %d-%b-%Y")
