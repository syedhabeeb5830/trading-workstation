"""
tests/test_triggers.py — Explicit entry-trigger contract (2026-07-02 deadlock fix)
==================================================================================
Locks in the trigger architecture so the "month of WAIT" deadlock can never
silently return:

  1. RR must NEVER gate classification (proven non-predictive OOS —
     analytics/rr_validation): a strong, well-located name with terrible
     geometric RR still classifies ACTION_NOW; a poor-RR name is never AVOID
     for RR alone.
  2. Price/volume confirmation is a first-class trigger: a confirmed breakout
     bar or pullback-reclaim bar converts an ARMED leader to SIGNAL_TRIGGERED.
  3. The setup gates still hold: non-leaders, exhausted leaders and extended
     names never trigger, confirmed or not.
"""

import pandas as pd
import pytest
from types import SimpleNamespace

from portfolio.lifecycle_states import evaluate_trigger, TriggerType
from scanner.actionability_engine import ActionabilityRanker
from screen.screen_runner import _entry_confirmation


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────
def _frame(closes, highs=None, vols=None):
    n = len(closes)
    idx = pd.date_range("2026-05-01", periods=n, freq="B", tz="Asia/Kolkata")
    highs = highs or [c * 1.01 for c in closes]
    lows = [c * 0.99 for c in closes]
    vols = vols or [100_000] * n
    return pd.DataFrame({"Open": closes, "High": highs, "Low": lows,
                         "Close": closes, "Volume": vols}, index=idx)


def _ctx(context="PULLBACK_SETUP", extension_pct=1.0):
    return SimpleNamespace(context=context, extension_pct=extension_pct)


# ─────────────────────────────────────────────────────────────────────────────
# 1) RR is never a gate in classification
# ─────────────────────────────────────────────────────────────────────────────
class TestClassifyNoRRVeto:
    def test_action_now_fires_without_rr(self):
        # composite 85, act 85, RR 0.8 (would have been vetoed pre-fix)
        cls = ActionabilityRanker._classify(85.0, 85.0, 0.8, _ctx())
        assert cls == "ACTION_NOW"

    def test_poor_rr_is_not_avoid(self):
        # strong name, tradable setup, RR 0.5 → WATCHLIST (never AVOID for RR)
        cls = ActionabilityRanker._classify(70.0, 70.0, 0.5, _ctx())
        assert cls == "WATCHLIST"

    def test_weak_composite_still_avoid(self):
        cls = ActionabilityRanker._classify(50.0, 60.0, 5.0, _ctx())
        assert cls == "AVOID"

    def test_no_setup_still_avoid(self):
        cls = ActionabilityRanker._classify(85.0, 85.0, 3.0, _ctx("NO_SETUP"))
        assert cls == "AVOID"

    def test_extended_still_extended(self):
        cls = ActionabilityRanker._classify(85.0, 85.0, 3.0,
                                            _ctx(extension_pct=9.0))
        assert cls == "EXTENDED"


# ─────────────────────────────────────────────────────────────────────────────
# 2) confirmation bars (last COMPLETE session)
# ─────────────────────────────────────────────────────────────────────────────
class TestEntryConfirmation:
    def test_pullback_reclaim_fires_on_reversal_bar(self):
        closes = [100 + i * 0.3 for i in range(28)]
        closes[-3], closes[-2], closes[-1] = 104, 102, 104.5   # dip then up-day
        highs = [c * 1.01 for c in closes]
        highs[-2] = 103.0                                       # prior high below close
        row = SimpleNamespace(entry_context="PULLBACK_SETUP", entry=104.0, stop=98.0)
        c = _entry_confirmation(row, _frame(closes, highs))
        assert c["reclaim"] is True

    def test_pullback_down_day_does_not_fire(self):
        closes = [100 + i * 0.3 for i in range(28)]
        closes[-2], closes[-1] = 102, 101.0                     # still falling
        row = SimpleNamespace(entry_context="PULLBACK_SETUP", entry=104.0, stop=98.0)
        c = _entry_confirmation(row, _frame(closes))
        assert c["reclaim"] is False

    def test_breakout_needs_volume(self):
        closes = [100] * 27 + [106]                             # close above pivot
        row = SimpleNamespace(entry_context="BREAKOUT_SETUP", entry=105.0, stop=95.0)
        strong = _entry_confirmation(row, _frame(closes, vols=[100_000] * 27 + [150_000]))
        weak = _entry_confirmation(row, _frame(closes, vols=[100_000] * 28))
        assert strong["breakout"] is True
        assert weak["breakout"] is False

    def test_broken_setup_never_confirms(self):
        closes = [100 + i * 0.3 for i in range(28)]
        row = SimpleNamespace(entry_context="PULLBACK_SETUP", entry=104.0,
                              stop=closes[-1] + 1)              # close below stop
        c = _entry_confirmation(row, _frame(closes))
        assert not c["reclaim"] and not c["breakout"]

    def test_insufficient_data_never_confirms(self):
        row = SimpleNamespace(entry_context="PULLBACK_SETUP", entry=104.0, stop=98.0)
        c = _entry_confirmation(row, _frame([100] * 5))
        assert not c["reclaim"] and not c["breakout"]


# ─────────────────────────────────────────────────────────────────────────────
# 3) lifecycle trigger contract
# ─────────────────────────────────────────────────────────────────────────────
class TestEvaluateTrigger:
    BASE = dict(classification="WATCHLIST", rs_status="MARKET_LEADER",
                entry_context="PULLBACK_SETUP", extension_pct=1.0, leader_weeks=2)

    def test_reclaim_confirm_triggers(self):
        d = evaluate_trigger(**self.BASE, reclaim_confirmed=True)
        assert d.triggered and d.trigger_type == TriggerType.RECLAIM_CONFIRM

    def test_breakout_confirm_triggers(self):
        d = evaluate_trigger(**{**self.BASE, "entry_context": "BREAKOUT_SETUP"},
                             breakout_confirmed=True)
        assert d.triggered and d.trigger_type == TriggerType.BREAKOUT_CONFIRM

    def test_unconfirmed_leader_is_armed_not_triggered(self):
        d = evaluate_trigger(**self.BASE)
        assert d.armed and not d.triggered

    def test_action_now_still_triggers(self):
        d = evaluate_trigger(**{**self.BASE, "classification": "ACTION_NOW"})
        assert d.triggered and d.trigger_type == TriggerType.ACTION_NOW

    def test_non_leader_never_triggers_even_confirmed(self):
        d = evaluate_trigger(**{**self.BASE, "rs_status": "NEUTRAL"},
                             reclaim_confirmed=True)
        assert not d.triggered and not d.armed

    def test_exhausted_leader_never_triggers_even_confirmed(self):
        d = evaluate_trigger(**{**self.BASE, "leader_weeks": 9},
                             reclaim_confirmed=True)
        assert not d.triggered

    def test_extended_never_triggers_even_confirmed(self):
        d = evaluate_trigger(**{**self.BASE, "extension_pct": 7.0},
                             breakout_confirmed=True, reclaim_confirmed=True)
        assert not d.triggered

    def test_avoid_class_never_triggers(self):
        d = evaluate_trigger(**{**self.BASE, "classification": "AVOID"},
                             reclaim_confirmed=True)
        assert not d.triggered and not d.armed
