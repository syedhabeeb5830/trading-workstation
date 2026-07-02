"""
portfolio/lifecycle_states.py — Canonical Asset Lifecycle State Machine
======================================================================
ONE unambiguous definition of where a name sits in the execution pipeline, so
the cockpit can never again say "no actionable setups" while simultaneously
emitting "entry-ready now, would buy" limit orders (the split-personality bug).

Lifecycle (forward-only, except CLOSED→WATCHLIST re-entry):

    WATCHLIST ──trigger fires──▶ SIGNAL_TRIGGERED ──order placed──▶ ACTIVE_ORDER
        ▲                                                               │
        │                                                          fill │
     re-arm                                                             ▼
    CLOSED ◀──exit──── OPEN_POSITION ◀──settle──── EXECUTED

The pivotal rule (the autopsy's Critical fix #2):

    A WATCHLIST name may ONLY advance to SIGNAL_TRIGGERED when an EXPLICIT
    trigger has fired. "Looks like a nice pullback" (good entry geometry) is
    NOT a trigger — it keeps the name ARMED on the WATCHLIST. The execution
    queue is fed by SIGNAL_TRIGGERED and beyond, never by WATCHLIST.

In this system the explicit trigger is the actionability engine's own
`classification == "ACTION_NOW"` verdict (its job is "tradable TODAY?"), or a
confirmed breakout/breakdown bar. If ACTION_NOW == 0 there are, by definition,
zero SIGNAL_TRIGGERED names → zero entry-ready orders. Contradiction gone.

Pure logic — no scores recomputed, no I/O. This GATES the decision; it does not
change any ranking/regime/composite math.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

_log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# STATES
# ─────────────────────────────────────────────────────────────────────────────
class AssetState(str, Enum):
    WATCHLIST        = "WATCHLIST"         # tracked; may be ARMED but no trigger yet
    SIGNAL_TRIGGERED = "SIGNAL_TRIGGERED"  # explicit trigger fired → enters exec queue
    ACTIVE_ORDER     = "ACTIVE_ORDER"      # order resting at broker (GTT / limit)
    EXECUTED         = "EXECUTED"          # order filled (pre-settlement bookkeeping)
    OPEN_POSITION    = "OPEN_POSITION"     # held in the book, managed by lifecycle
    CLOSED           = "CLOSED"            # exited; can re-arm to WATCHLIST

    def __str__(self) -> str:  # so f-strings print "WATCHLIST" not "AssetState.WATCHLIST"
        return self.value


class TriggerType(str, Enum):
    NONE              = "NONE"               # armed, nothing fired
    ACTION_NOW        = "ACTION_NOW"         # the actionability engine fired (the system's trigger)
    BREAKOUT_CONFIRM  = "BREAKOUT_CONFIRM"   # price closed above the breakout level on volume
    RECLAIM_CONFIRM   = "RECLAIM_CONFIRM"    # pullback reversal bar: up-day close above prior high
    BREAKDOWN_CONFIRM = "BREAKDOWN_CONFIRM"  # (short side / exit trigger)
    MANUAL            = "MANUAL"             # trader override


# Allowed forward transitions. Anything not listed is rejected by `transition`.
_ALLOWED: dict[AssetState, set[AssetState]] = {
    AssetState.WATCHLIST:        {AssetState.SIGNAL_TRIGGERED},
    AssetState.SIGNAL_TRIGGERED: {AssetState.ACTIVE_ORDER, AssetState.WATCHLIST},   # can disarm
    AssetState.ACTIVE_ORDER:     {AssetState.EXECUTED, AssetState.WATCHLIST},        # fill or cancel
    AssetState.EXECUTED:         {AssetState.OPEN_POSITION},
    AssetState.OPEN_POSITION:    {AssetState.CLOSED},
    AssetState.CLOSED:           {AssetState.WATCHLIST},
}

# States from which capital can actually be committed (the execution queue).
_EXECUTION_QUEUE = {AssetState.SIGNAL_TRIGGERED, AssetState.ACTIVE_ORDER}


# ─────────────────────────────────────────────────────────────────────────────
# TRIGGER LOGIC  (the heart of the fix)
# ─────────────────────────────────────────────────────────────────────────────
_LEADER_TIERS = {"MARKET_LEADER", "SECTOR_LEADER", "EMERGING_LEADER"}
_TRADABLE_CTX = {"PULLBACK_SETUP", "BREAKOUT_SETUP", "TREND_CONTINUATION"}


@dataclass(frozen=True)
class TriggerDecision:
    triggered:    bool
    trigger_type: TriggerType
    armed:        bool          # passes all setup gates but is waiting for a trigger
    reason:       str


def evaluate_trigger(
    *, classification: str, rs_status: str, entry_context: str,
    extension_pct: float = 0.0, leader_weeks: int = 0,
    max_extension_pct: float = 5.0, exhaustion_weeks: int = 8,
    breakout_confirmed: bool = False, reclaim_confirmed: bool = False,
) -> TriggerDecision:
    """Decide whether a screened name has an EXPLICIT trigger to enter the
    execution queue, or is merely ARMED on the watchlist.

    Setup gates (must all pass to even be ARMED — these mirror the proven edge):
      • RS leadership (the only directionally-proven signal),
      • not extended / not chasing,
      • not an exhausted (8+ wk) leader,
      • a tradable entry context.

    Trigger (must ALSO hold to advance to SIGNAL_TRIGGERED):
      • classification == ACTION_NOW  (the actionability engine's own
        "tradable today" verdict), OR
      • a confirmed breakout bar (close above pivot on expanded volume), OR
      • a confirmed pullback reclaim (up-day close above the prior bar's high
        at a tradable location — the "1 up-day confirm" reversal bar).

    2026-07-02 deadlock fix: ACTION_NOW alone was the only live trigger, and its
    (composite ≥ 80 ∧ act ≥ 80 ∧ RR ≥ 2) definition was a near-empty set — RR is
    non-predictive OOS and anti-correlated with momentum composites, so the gate
    fired 196× in 94,637 obs and 0× for a month in a BULL regime. Price/volume
    confirmation (computed by screen_runner on the last COMPLETE session) is now
    a first-class trigger, so armed leaders convert to orders mechanically.
    """
    cls = (classification or "").upper()
    rs  = (rs_status or "").upper()
    ctx = (entry_context or "").upper()

    # ── Setup gates: is this even ARMED? ──────────────────────────────────────
    if cls in ("AVOID", "EXTENDED"):
        return TriggerDecision(False, TriggerType.NONE, False,
                               f"{cls.lower()} — not a watchlist candidate")
    if rs not in _LEADER_TIERS:
        return TriggerDecision(False, TriggerType.NONE, False,
                               f"not an RS leader yet ({rs.replace('_', ' ').lower()}) "
                               f"— unproven edge")
    if leader_weeks >= exhaustion_weeks:
        return TriggerDecision(False, TriggerType.NONE, False,
                               f"mature leader ({leader_weeks} wks) — mean-reversion risk")
    if extension_pct > max_extension_pct:
        return TriggerDecision(False, TriggerType.NONE, False,
                               f"extended {extension_pct:.0f}% — wait for pullback")
    if ctx not in _TRADABLE_CTX:
        return TriggerDecision(False, TriggerType.NONE, False,
                               f"no tradable setup ({ctx.lower()}) — keep watching")

    # ── ARMED. Now: has an explicit trigger fired? ────────────────────────────
    if cls == "ACTION_NOW":
        return TriggerDecision(True, TriggerType.ACTION_NOW, True,
                               "ACTION_NOW — actionability engine flagged tradable today")
    if breakout_confirmed:
        return TriggerDecision(True, TriggerType.BREAKOUT_CONFIRM, True,
                               "breakout confirmed on volume")
    if reclaim_confirmed:
        return TriggerDecision(True, TriggerType.RECLAIM_CONFIRM, True,
                               "pullback reclaim confirmed (up-day close above prior high)")

    # ARMED but NOT triggered → stays on the watchlist, NOT in the exec queue.
    return TriggerDecision(False, TriggerType.NONE, True,
                           "armed — proven leader at a setup, but no trigger fired "
                           "— wait for the breakout/reclaim confirmation bar")


# ─────────────────────────────────────────────────────────────────────────────
# STATE RESOLUTION
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class AssetLifecycle:
    symbol:       str
    state:        AssetState
    trigger:      TriggerType = TriggerType.NONE
    reason:       str = ""
    history:      list[str] = field(default_factory=list)

    @property
    def is_execution_ready(self) -> bool:
        """True only when the name may legitimately have a live/ready order."""
        return self.state in _EXECUTION_QUEUE

    def transition(self, to: AssetState, *, why: str = "") -> bool:
        """Apply a guarded forward transition. Returns False (and logs) on an
        illegal jump — e.g. WATCHLIST → ACTIVE_ORDER without a trigger."""
        if to not in _ALLOWED.get(self.state, set()):
            _log.warning("%s: illegal transition %s → %s rejected (%s)",
                         self.symbol, self.state, to, why or "no reason")
            return False
        self.history.append(f"{self.state} → {to}: {why}")
        self.state = to
        self.reason = why
        return True


def classify(
    *, symbol: str, held: bool = False, has_open_order: bool = False,
    classification: str = "", rs_status: str = "", entry_context: str = "",
    extension_pct: float = 0.0, leader_weeks: int = 0,
    breakout_confirmed: bool = False, reclaim_confirmed: bool = False,
) -> AssetLifecycle:
    """Resolve a screened/held name into its canonical lifecycle state.

    Precedence: a real broker position/order is ground truth (OPEN_POSITION /
    ACTIVE_ORDER) and overrides the screen. Otherwise the screen decides between
    WATCHLIST (armed or not) and SIGNAL_TRIGGERED."""
    if held:
        return AssetLifecycle(symbol, AssetState.OPEN_POSITION,
                              reason="held in book — manage via lifecycle engine")
    if has_open_order:
        return AssetLifecycle(symbol, AssetState.ACTIVE_ORDER,
                              reason="resting order at broker")

    d = evaluate_trigger(
        classification=classification, rs_status=rs_status,
        entry_context=entry_context, extension_pct=extension_pct,
        leader_weeks=leader_weeks, breakout_confirmed=breakout_confirmed,
        reclaim_confirmed=reclaim_confirmed)

    if d.triggered:
        return AssetLifecycle(symbol, AssetState.SIGNAL_TRIGGERED,
                              trigger=d.trigger_type, reason=d.reason)
    return AssetLifecycle(symbol, AssetState.WATCHLIST,
                          trigger=TriggerType.NONE, reason=d.reason)


def _row_decision(row, leader_weeks: Optional[dict],
                  confirm: Optional[dict]) -> TriggerDecision:
    """Shared row → TriggerDecision resolution for the convenience gates.
    `confirm` is screen_runner's per-ticker price/volume confirmation:
    {"breakout": bool, "reclaim": bool, ...} computed on the last COMPLETE bar."""
    lw = (leader_weeks or {}).get(getattr(row, "ticker", ""), 0)
    c = confirm or {}
    return evaluate_trigger(
        classification=getattr(row, "classification", ""),
        rs_status=getattr(row, "rs_status", ""),
        entry_context=getattr(row, "entry_context", ""),
        extension_pct=float(getattr(row, "extension_pct", 0.0) or 0.0),
        leader_weeks=int(lw or 0),
        breakout_confirmed=bool(c.get("breakout")),
        reclaim_confirmed=bool(c.get("reclaim")))


def decide(row, leader_weeks: Optional[dict] = None,
           confirm: Optional[dict] = None) -> TriggerDecision:
    """Public row → TriggerDecision (the ONE gate evaluation). Exposes the full
    decision — triggered/armed flags, trigger type AND the exact human-readable
    reason — so the cockpit's DECISION TRACE prints the same logic that gates
    the trade, never a re-derived approximation."""
    if row is None:
        return TriggerDecision(False, TriggerType.NONE, False, "no data")
    return _row_decision(row, leader_weeks, confirm)


def is_signal_triggered(row, leader_weeks: Optional[dict] = None,
                        confirm: Optional[dict] = None) -> bool:
    """Convenience gate for an ActionabilityRow: True ⇔ the name has an explicit
    trigger and may enter the execution queue. Used by screen_runner to feed
    BUY-TODAY. A name that is merely ARMED returns False."""
    if row is None:
        return False
    return _row_decision(row, leader_weeks, confirm).triggered


def is_armed(row, leader_weeks: Optional[dict] = None,
             confirm: Optional[dict] = None) -> bool:
    """True ⇔ the name passes every setup gate (proven leader, clean entry, not
    extended/exhausted) but is WAITING for a trigger. These names get exact
    CONDITIONAL orders (GTT trigger/stop/targets/qty) — placed, never chased."""
    if row is None:
        return False
    d = _row_decision(row, leader_weeks, confirm)
    return d.armed and not d.triggered
