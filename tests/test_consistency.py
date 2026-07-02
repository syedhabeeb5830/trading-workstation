"""
tests/test_consistency.py — One-truth pipeline contracts (2026-07-02 audit)
===========================================================================
Locks in the consistency fixes from the Chief-Quant audit so the "board says
#1, plan says something else, nobody knows why" class of bug can never
silently return:

  1. RR carries ZERO ranking weight anywhere (proven non-predictive OOS):
     neither actionability nor conviction may move when RR changes.
  2. Board→plan alignment: within an RS tier, conviction is strictly monotone
     in actionability — the plan can only reorder the board BETWEEN RS tiers
     (the one validated reason), never within one.
  3. Regime one-truth: BEAR/VOLATILE regimes allocate ZERO fresh exposure in
     the portfolio engine — matching the committee's E4 stand-aside gate.
  4. The allocator explains every skip (decision-trace contract).
  5. lifecycle decide() is the single public gate evaluation and its reasons
     are non-empty for every path.
"""

import pytest
from types import SimpleNamespace

from scanner.actionability_engine import ACT_WEIGHTS, ActionabilityScorer
from portfolio.portfolio_engine import (PositionSizer, RiskBudgetAllocator,
                                        PortfolioCandidate, REGIME_EXPOSURE,
                                        MIN_POSITION_SIZE)
from portfolio.lifecycle_states import decide


# ─────────────────────────────────────────────────────────────────────────────
# 1) RR has zero ranking weight everywhere
# ─────────────────────────────────────────────────────────────────────────────
class TestRRZeroWeight:
    def test_act_weights_rr_zero(self):
        assert ACT_WEIGHTS["risk_reward"] == 0.0
        assert abs(sum(ACT_WEIGHTS.values()) - 1.0) < 1e-9

    def test_actionability_insensitive_to_rr(self):
        s = ActionabilityScorer()
        lo = s.score(composite=85.0, entry_quality=90.0, risk_reward=0.0)["score"]
        hi = s.score(composite=85.0, entry_quality=90.0, risk_reward=100.0)["score"]
        assert lo == hi

    def test_conviction_insensitive_to_rr(self):
        p = PositionSizer()
        lo = p.conviction("MARKET_LEADER", 80.0, rr=0.5, grade="A")
        hi = p.conviction("MARKET_LEADER", 80.0, rr=5.0, grade="A")
        assert lo == hi


# ─────────────────────────────────────────────────────────────────────────────
# 2) Board→plan alignment: conviction monotone in actionability within RS tier
# ─────────────────────────────────────────────────────────────────────────────
class TestRankingAlignment:
    def test_conviction_monotone_within_rs_tier(self):
        p = PositionSizer()
        for rs in ("MARKET_LEADER", "SECTOR_LEADER", "EMERGING_LEADER", "NEUTRAL"):
            prev = -1.0
            for act in (40.0, 55.0, 70.0, 85.0, 95.0):
                conv = p.conviction(rs, act, rr=2.0, grade="B+")
                assert conv > prev, (rs, act)
                prev = conv

    def test_rs_tier_is_the_only_promoter(self):
        """A market leader with a WEAKER board score may out-convict a neutral
        name (the validated RS-first rule) — but the secondary grade modifier
        (±5) must never flip a MATERIAL (15-point) actionability gap."""
        p = PositionSizer()
        strong_neutral = p.conviction("NEUTRAL", 90.0, rr=2.0, grade="A+")
        weak_leader = p.conviction("MARKET_LEADER", 70.0, rr=2.0, grade="C")
        assert weak_leader > strong_neutral        # RS promotion is allowed
        same_rs_hi = p.conviction("SECTOR_LEADER", 85.0, rr=2.0, grade="C")
        same_rs_lo = p.conviction("SECTOR_LEADER", 70.0, rr=2.0, grade="A+")
        assert same_rs_hi > same_rs_lo             # grade can't flip 15 act pts


# ─────────────────────────────────────────────────────────────────────────────
# 3) Regime one-truth: zero fresh exposure in BEAR/VOLATILE
# ─────────────────────────────────────────────────────────────────────────────
class TestRegimeOneTruth:
    def test_bear_and_volatile_zero_exposure(self):
        for reg in ("WEAK_BEAR", "STRONG_BEAR", "VOLATILE"):
            assert REGIME_EXPOSURE[reg] == 0, reg

    def test_bull_family_still_deploys(self):
        assert REGIME_EXPOSURE["STRONG_BULL"] == 100
        assert REGIME_EXPOSURE["BULL"] > 0
        assert REGIME_EXPOSURE["NEUTRAL"] > 0


# ─────────────────────────────────────────────────────────────────────────────
# 4) Allocator explains every skip (decision-trace contract)
# ─────────────────────────────────────────────────────────────────────────────
def _cand(sym, conviction, size, sector="Tech", cluster=None):
    return PortfolioCandidate(
        ticker=f"{sym}.NS", symbol=sym, sector=sector, rs_status="MARKET_LEADER",
        classification="ACTION_NOW", conviction=conviction,
        actionability_score=80.0, composite_score=80.0, grade="A", rr=2.0,
        atr_pct=2.0, cluster_id=cluster or f"{sym}.NS", desired_size=size)


class TestAllocatorSkipReasons:
    def test_max_positions_skip_reason(self):
        a, b = _cand("AAA", 90, 8.0), _cand("BBB", 80, 8.0, sector="Pharma")
        chosen, _ = RiskBudgetAllocator().allocate([a, b], exposure=100,
                                                   max_positions=1)
        assert [c.symbol for c in chosen] == ["AAA"]
        assert "max positions" in b.skip_reason

    def test_sector_cap_skip_reason(self):
        a, b = _cand("AAA", 90, 20.0), _cand("BBB", 80, 20.0)   # same sector
        chosen, _ = RiskBudgetAllocator().allocate([a, b], exposure=100,
                                                   max_sector=20.0)
        assert [c.symbol for c in chosen] == ["AAA"]
        assert "sector cap" in b.skip_reason

    def test_dust_size_skip_reason(self):
        a = _cand("AAA", 20, 0.0)
        chosen, _ = RiskBudgetAllocator().allocate([a], exposure=100)
        assert not chosen
        assert "sizing floor" in a.skip_reason


# ─────────────────────────────────────────────────────────────────────────────
# 5) decide() — single public gate evaluation, always with a reason
# ─────────────────────────────────────────────────────────────────────────────
def _row(**kw):
    base = dict(ticker="AAA.NS", symbol="AAA", classification="WATCHLIST",
                rs_status="MARKET_LEADER", entry_context="PULLBACK_SETUP",
                extension_pct=1.0)
    base.update(kw)
    return SimpleNamespace(**base)


class TestDecidePublicGate:
    def test_none_row_is_safe(self):
        d = decide(None)
        assert not d.triggered and not d.armed and d.reason

    def test_armed_leader_has_reason(self):
        d = decide(_row())
        assert d.armed and not d.triggered and "no trigger" in d.reason

    def test_confirmed_reclaim_triggers(self):
        d = decide(_row(), confirm={"reclaim": True})
        assert d.triggered

    def test_non_leader_rejected_with_reason(self):
        d = decide(_row(rs_status="NEUTRAL"))
        assert not d.armed and not d.triggered
        assert "not an RS leader" in d.reason

    def test_screen_runner_delegates_to_lifecycle(self):
        """The duplicated gate copy in screen_runner must stay a pure delegate."""
        from screen.screen_runner import (_is_genuine_entry, _is_armed,
                                          _is_signal_triggered)
        r = _row()
        assert _is_genuine_entry(r) is True
        assert _is_armed(r) is True
        assert _is_signal_triggered(r) is False
        assert _is_signal_triggered(r, confirm={"reclaim": True}) is True
        assert _is_genuine_entry(_row(rs_status="LAGGARD")) is False
