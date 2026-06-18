"""
analytics/adaptive_risk.py — Adaptive Risk Engine + Strategy Kill Switch
========================================================================
Capital-preservation first. The system already KNOWS when its edge is weak
(EDGE HEALTH) and what regime it is in — so risk should not be a constant 1%.

RISK MODE (survival-first: take the MOST CONSERVATIVE of three reads)
  • recent 8-week leader excess      (current expectancy)
  • structural edge-health verdict   (ALIVE / MIXED / DYING)
  • market regime                    (BULL / NEUTRAL / BEAR)

  STRONG   → risk 1.0% · max heat 6.0%
  MIXED    → risk 0.5% · max heat 3.0%
  NEGATIVE → risk 0.25% · max heat 1.5%
(scaled by the trader's configured base risk; the table assumes base = 1.0%.)

KILL SWITCH — disable NEW entries when survival is threatened:
  • rolling 20-trade expectancy < 0       (your real trades stopped paying)
  • realized max drawdown > limit          (default 10%)
  • edge health negative + recent ≤ 0       (environment turned hostile)
Recovery criteria are returned so reactivation is rule-based, not emotional.

Pure logic — no scores, no indicators. Consumes edge_health + the live journal.
"""

from __future__ import annotations

# mode ordering: most-conservative wins
_ORDER = {"STRONG": 3, "MIXED": 2, "NEGATIVE": 1}
_PARAMS = {                       # (risk_pct multiplier, max_heat_pct at base=1.0)
    "STRONG":   (1.00, 6.0),
    "MIXED":    (0.50, 3.0),
    "NEGATIVE": (0.25, 1.5),
}


def _mode_from_recent(recent) -> str:
    if recent is None:
        return "MIXED"
    if recent >= 2.0:
        return "STRONG"
    if recent < 0.0:
        return "NEGATIVE"
    return "MIXED"


def _mode_from_verdict(verdict: str) -> str:
    v = (verdict or "").upper()
    if "ALIVE" in v:
        return "STRONG"
    if "DYING" in v or "DEAD" in v or "NEGATIVE" in v:
        return "NEGATIVE"
    return "MIXED"


def _mode_from_regime(regime: str) -> str:
    r = (regime or "").upper()
    if "BEAR" in r:
        return "NEGATIVE"
    if "NEUTRAL" in r or "RANGE" in r or "VOLATILE" in r:
        return "MIXED"
    return "STRONG"


def risk_mode(edge_h: dict | None, regime: str, base_risk_pct: float = 1.0) -> dict:
    """Most-conservative-of-three risk mode → risk%/trade + max book heat%."""
    recent = (edge_h or {}).get("recent_excess")
    verdict = (edge_h or {}).get("verdict", "")
    m_recent = _mode_from_recent(recent)
    m_verdict = _mode_from_verdict(verdict)
    m_regime = _mode_from_regime(regime)
    mode = min((m_recent, m_verdict, m_regime), key=lambda m: _ORDER[m])
    mult, heat = _PARAMS[mode]
    binding = min(
        [("recent edge", m_recent), ("structural edge", m_verdict), ("regime", m_regime)],
        key=lambda kv: _ORDER[kv[1]])[0]
    return {
        "mode": mode,
        "risk_pct": round(base_risk_pct * mult, 3),
        "max_heat_pct": round(heat * (base_risk_pct / 1.0), 2),
        "binding": binding,
        "reason": (f"recent excess {recent if recent is None else f'{recent:+.1f}%'} · "
                   f"{verdict.split('—')[0].strip()} · regime {regime} "
                   f"→ {mode} (driven by {binding})"),
    }


def kill_switch(edge_h: dict | None, journal_stats: dict | None,
                max_dd_limit: float = 10.0, min_trades: int = 10) -> dict:
    """Should NEW entries be disabled? Returns paused flag + reasons + recovery."""
    reasons, recovery = [], []
    js = journal_stats or {}
    n = js.get("n_closed", 0)

    # 1) rolling 20-trade expectancy < 0 (needs enough real trades)
    r20 = js.get("rolling20_expectancy_R")
    if n >= min_trades and r20 is not None and r20 < 0:
        reasons.append(f"rolling-20 expectancy {r20:+.2f}R < 0 (real trades stopped paying)")
        recovery.append("rolling-20 expectancy back ≥ +0.1R")

    # 2) realized drawdown breach
    dd = js.get("max_dd_pct")
    if dd is not None and dd <= -abs(max_dd_limit):
        reasons.append(f"realized drawdown {dd:.1f}% breached −{max_dd_limit:.0f}% limit")
        recovery.append(f"drawdown recovers above −{max_dd_limit/2:.0f}%")

    # 3) hostile environment: structural edge negative AND recent ≤ 0
    recent = (edge_h or {}).get("recent_excess")
    verdict = (edge_h or {}).get("verdict", "")
    if "DYING" in verdict.upper() and (recent is not None and recent <= 0):
        reasons.append(f"edge health DYING and recent excess {recent:+.1f}% ≤ 0")
        recovery.append("recent 8-week excess back > 0 for 2+ weeks")

    return {
        "paused": bool(reasons),
        "reasons": reasons,
        "recovery": recovery or ["—"],
    }
