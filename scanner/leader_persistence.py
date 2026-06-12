"""
scanner/leader_persistence.py — Market Leader Persistence (Improvement 3)
========================================================================
Phase 3 ranks leadership at a point in time, but DURATION matters: a stock that
just entered the TOP_10 RS bucket this week is a very different animal from one
that has held TOP_10 for 12 straight weeks. Persistent leaders tend to keep
leading; fresh entrants are noisier.

This module computes `leader_weeks` — the number of consecutive most-recent weeks
a ticker has sat in the top RS bucket — from the persisted weekly RS history
(Journal/rs_history), and maps it to a 0-100 persistence score:

    1 wk → 20 · 2 wk → 35 · 4 wk → 60 · 8 wk → 80 · 12+ wk → 100

It is a pure, read-only enrichment: it consumes RS snapshots and never modifies
the RS or composite engines (which the user marked "do not touch"). Whether it
should feed scoring is an EVIDENCE question — it is validated in the rolling
framework before any wiring.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from scanner.relative_strength_engine import RelativeStrengthSnapshot

_RS_HISTORY = Path("Journal/rs_history")
DEFAULT_TOP_PCT = 0.10          # "leader" = top-decile RS (TOP_10 bucket)


def persistence_score(weeks: Optional[int]) -> float:
    """Map consecutive leader-weeks to a 0-100 score (per the agreed ramp)."""
    if not weeks or weeks <= 0:
        return 0.0
    if weeks >= 12:
        return 100.0
    if weeks >= 8:
        return 80.0
    if weeks >= 4:
        return 60.0
    if weeks >= 2:
        return 35.0
    return 20.0


class LeaderPersistenceTracker:
    """Computes consecutive top-bucket streaks from an ordered RS-snapshot series."""

    def __init__(self, top_pct: float = DEFAULT_TOP_PCT):
        self.top_pct = top_pct

    def _top_set(self, snap: RelativeStrengthSnapshot) -> set[str]:
        return {r.ticker for r in snap.rows
                if r.percentile is not None and r.percentile < self.top_pct}

    def streaks(self, ordered_snaps: list[RelativeStrengthSnapshot]) -> dict[str, int]:
        """{ticker: consecutive weeks in the top bucket, counting back from the latest}.

        Only tickers currently in the top bucket get a positive streak.
        """
        if not ordered_snaps:
            return {}
        top_sets = [self._top_set(s) for s in ordered_snaps]
        out: dict[str, int] = {}
        for t in top_sets[-1]:
            w = 0
            for s in reversed(top_sets):
                if t in s:
                    w += 1
                else:
                    break
            out[t] = w
        return out

    def from_history(self, current: Optional[RelativeStrengthSnapshot] = None,
                     history_dir: Path = _RS_HISTORY) -> dict[str, int]:
        """Load weekly RS snapshots from disk (chronological) + optional current
        snapshot, and return leader_weeks per ticker."""
        snaps: list[RelativeStrengthSnapshot] = []
        if history_dir.exists():
            for p in sorted(history_dir.glob("*.json")):
                try:
                    snaps.append(RelativeStrengthSnapshot.from_dict(json.loads(p.read_text())))
                except Exception:
                    continue
        if current is not None:
            if snaps and snaps[-1].week_id == current.week_id:
                snaps[-1] = current          # replace same-week duplicate
            else:
                snaps.append(current)
        return self.streaks(snaps)
