"""
algo/filters.py — Institutional Breakout Quality & Environment Filters
=======================================================================
Implements the key transition from:
  reactive breakout trading (retail)      — price crossed level
  to:
  institutional participation detection   — IS THIS REAL DISPLACEMENT?

Each filter is independent and composable. In backtesting they are evaluated
as a gate before entry. In live trading, each failed filter is logged
individually to build the expectancy / playbook database over time.

Filters implemented:
  BreakoutQualityFilter  — candle body, close location, relative volume
  LiquiditySweepDetector — wick rejection, failed displacement
  RelativeStrengthFilter — stock outperforming/underperforming NIFTY intraday
  TimeWindowFilter       — prime execution window (09:45–13:00)
  GapFilter              — skip news-driven gap opens

Design principle:
  Every filter has a reason string so you can log WHY a trade was skipped.
  This becomes your playbook dataset. Over 100+ skips you'll see patterns.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import time, date, timedelta
from statistics import mean
from typing import Optional

from algo.candles import Candle


# ─────────────────────────────────────────────────────────────────────────────
# RESULT TYPE
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class FilterResult:
    """The outcome of a single filter check."""
    passed:       bool
    filter_name:  str
    reason:       str
    value:        float = 0.0       # the measured value (e.g. rvol=1.8)
    threshold:    float = 0.0       # the minimum/maximum required


@dataclass
class GateResult:
    """Composite result from running all filters."""
    all_passed:   bool
    passed:       list[FilterResult]
    failed:       list[FilterResult]
    block_reason: str = ""

    @property
    def first_failure(self) -> Optional[FilterResult]:
        return self.failed[0] if self.failed else None


def run_gate(results: list[FilterResult]) -> GateResult:
    """Aggregate individual FilterResults into a single gate decision."""
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]
    return GateResult(
        all_passed=(len(failed) == 0),
        passed=passed,
        failed=failed,
        block_reason=failed[0].filter_name if failed else "",
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1.  BREAKOUT QUALITY ENGINE
# ─────────────────────────────────────────────────────────────────────────────
class BreakoutQualityFilter:
    """
    Scores a breakout candle on three dimensions and rejects weak setups.

    GOOD breakout (institutional displacement):
      • Large body — candle is mostly directional, not a wick
      • Close near extreme — closed in top 35% of range (long) or bottom (short)
      • Volume expansion — at least 1.5× average of prior 5 candles

    BAD breakout (retail trap / liquidity grab):
      • Tiny body with huge wicks
      • Closes in the middle of its own range
      • Below-average or flat volume

    Parameters
    ----------
    min_body_pct   : 0.55 → body must be ≥55% of candle total range
    min_close_loc  : 0.65 → for long, close must be in top 35% of candle range
    min_rvol       : 1.5  → volume must be ≥1.5× prior 5-candle average
    min_prior_bars : 3    → minimum prior candles required to calculate avg volume
    """

    def __init__(
        self,
        min_body_pct:   float = 0.55,
        min_close_loc:  float = 0.65,
        min_rvol:       float = 1.5,
        min_prior_bars: int   = 3,
    ):
        self.min_body_pct  = min_body_pct
        self.min_close_loc = min_close_loc
        self.min_rvol      = min_rvol
        self.min_prior     = min_prior_bars

    def check(
        self,
        candle:    Candle,
        direction: str,           # "LONG" or "SHORT"
        prior:     list[Candle],  # bars BEFORE this candle (for avg vol)
    ) -> FilterResult:
        rng = candle.high - candle.low
        if rng <= 0:
            return FilterResult(False, "BreakoutQuality",
                                "zero-range candle — skip")

        body        = abs(candle.close - candle.open)
        body_pct    = body / rng
        close_loc   = (candle.close - candle.low) / rng  # 0=bottom, 1=top

        # ── Body check ──────────────────────────────────────────────────────
        if body_pct < self.min_body_pct:
            return FilterResult(
                False, "BreakoutQuality",
                f"weak body {body_pct:.0%} — mostly wick, not real displacement",
                value=body_pct, threshold=self.min_body_pct,
            )

        # ── Close location check ─────────────────────────────────────────────
        if direction == "LONG" and close_loc < self.min_close_loc:
            return FilterResult(
                False, "BreakoutQuality",
                f"close at {close_loc:.0%} of range — wick rejection on long",
                value=close_loc, threshold=self.min_close_loc,
            )
        if direction == "SHORT" and close_loc > (1.0 - self.min_close_loc):
            return FilterResult(
                False, "BreakoutQuality",
                f"close at {close_loc:.0%} of range — wick rejection on short",
                value=1.0 - close_loc, threshold=self.min_close_loc,
            )

        # ── Relative volume check ────────────────────────────────────────────
        vols = [c.volume for c in prior[-5:] if c.volume > 0]
        if len(vols) >= self.min_prior:
            avg_vol = mean(vols)
            rvol    = candle.volume / avg_vol if avg_vol > 0 else 0.0
            if rvol < self.min_rvol:
                return FilterResult(
                    False, "BreakoutQuality",
                    f"RVOL {rvol:.1f}× — no institutional volume confirmation",
                    value=rvol, threshold=self.min_rvol,
                )

        return FilterResult(
            True, "BreakoutQuality",
            f"body={body_pct:.0%} loc={close_loc:.0%}",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2.  LIQUIDITY SWEEP DETECTOR
# ─────────────────────────────────────────────────────────────────────────────
class LiquiditySweepDetector:
    """
    Detects stop-hunt / liquidity sweep patterns on the breakout candle.

    Institutions frequently:
      1. Push price above a well-known level (e.g. ORB high) to trigger
         retail buy orders and flush short stops
      2. Immediately reverse — leaving a long upper wick
      3. Then sell into the retail longs as price collapses

    Detection (same-candle, no lookahead):
      For LONG breakout:
        upper_wick = candle.high - max(close, open)
        if upper_wick / total_range > max_wick_pct → sweep signature

      For SHORT breakdown:
        lower_wick = min(close, open) - candle.low
        if lower_wick / total_range > max_wick_pct → sweep signature

    Parameters
    ----------
    max_wick_pct : 0.35 → upper/lower wick must be <35% of candle range
    """

    def __init__(self, max_wick_pct: float = 0.35):
        self.max_wick_pct = max_wick_pct

    def check(
        self,
        candle:    Candle,
        direction: str,
    ) -> FilterResult:
        """Returns passed=True if NO sweep detected (safe to enter)."""
        rng = candle.high - candle.low
        if rng <= 0:
            return FilterResult(True, "SweepDetector", "zero range")

        if direction == "LONG":
            rejection_wick = candle.high - max(candle.close, candle.open)
            wick_pct       = rejection_wick / rng
            if wick_pct > self.max_wick_pct:
                return FilterResult(
                    False, "SweepDetector",
                    f"upper wick {wick_pct:.0%} — probable stop-hunt, "
                    f"institutions swept retail longs",
                    value=wick_pct, threshold=self.max_wick_pct,
                )
        else:  # SHORT
            rejection_wick = min(candle.close, candle.open) - candle.low
            wick_pct       = rejection_wick / rng
            if wick_pct > self.max_wick_pct:
                return FilterResult(
                    False, "SweepDetector",
                    f"lower wick {wick_pct:.0%} — probable stop-hunt, "
                    f"institutions swept retail shorts",
                    value=wick_pct, threshold=self.max_wick_pct,
                )

        return FilterResult(True, "SweepDetector", "no wick rejection")


# ─────────────────────────────────────────────────────────────────────────────
# 3.  RELATIVE STRENGTH FILTER
# ─────────────────────────────────────────────────────────────────────────────
class RelativeStrengthFilter:
    """
    Compares the stock's intraday % move to NIFTY50.

    Institutional logic:
      When institutions are accumulating a stock, it shows RELATIVE STRENGTH
      — it rises even when the index is flat or down.
      This is one of the highest-ROI filters available because it detects
      REAL institutional money flow, not just price level crossing.

    Rules:
      LONG valid only if:  stock_pct_change > nifty_pct_change  (outperforming)
      SHORT valid only if: stock_pct_change < nifty_pct_change  (underperforming)

    Parameters
    ----------
    min_rs_edge : minimum outperformance required (default 0.0 = any positive RS)
    """

    def __init__(self, min_rs_edge: float = 0.0):
        self.min_rs_edge = min_rs_edge

    def check(
        self,
        stock_open:  float,
        stock_close: float,
        nifty_open:  float,
        nifty_close: float,
        direction:   str,
    ) -> FilterResult:
        if stock_open <= 0 or nifty_open <= 0:
            # No NIFTY data available — let trade through (don't block on missing data)
            return FilterResult(True, "RelativeStrength",
                                "no NIFTY data — filter bypassed")

        stock_chg = (stock_close - stock_open) / stock_open * 100
        nifty_chg = (nifty_close - nifty_open) / nifty_open * 100
        rs        = stock_chg - nifty_chg  # positive = outperforming

        if direction == "LONG" and rs < self.min_rs_edge:
            return FilterResult(
                False, "RelativeStrength",
                f"stock {stock_chg:+.2f}% vs NIFTY {nifty_chg:+.2f}% "
                f"(RS={rs:+.2f}%) — underperforming index, fighting tape",
                value=rs, threshold=self.min_rs_edge,
            )

        if direction == "SHORT" and rs > -self.min_rs_edge:
            return FilterResult(
                False, "RelativeStrength",
                f"stock {stock_chg:+.2f}% vs NIFTY {nifty_chg:+.2f}% "
                f"(RS={rs:+.2f}%) — outperforming index on short attempt",
                value=rs, threshold=-self.min_rs_edge,
            )

        return FilterResult(
            True, "RelativeStrength",
            f"RS edge = {rs:+.2f}%",
            value=rs,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4.  TIME WINDOW FILTER
# ─────────────────────────────────────────────────────────────────────────────
class TimeWindowFilter:
    """
    Restricts entries to the institutional prime execution window.

    Default window: 09:45–13:00

    Why:
      Before 09:45 — Opening range manipulation still active. Institutions
        are still placing orders, price discovery isn't real.
      After 13:00  — Lunch-hour volume evaporates. Spreads widen slightly.
        Pre-close institutional positioning creates unpredictable reversals.

    The 09:45–13:00 window captures the vast majority of genuine ORB moves
    while filtering out the two highest-risk periods.
    """

    def __init__(self, start: str = "09:45", end: str = "13:00"):
        self.start = time(*map(int, start.split(":")))
        self.end   = time(*map(int, end.split(":")))

    def check(self, ts_time: time) -> FilterResult:
        if ts_time < self.start:
            return FilterResult(
                False, "TimeWindow",
                f"{ts_time.strftime('%H:%M')} before prime window "
                f"(starts {self.start.strftime('%H:%M')}) — opening manipulation zone",
            )
        if ts_time >= self.end:
            return FilterResult(
                False, "TimeWindow",
                f"{ts_time.strftime('%H:%M')} after prime window "
                f"(ends {self.end.strftime('%H:%M')}) — low participation zone",
            )
        return FilterResult(True, "TimeWindow",
                            f"within prime window {self.start.strftime('%H:%M')}–"
                            f"{self.end.strftime('%H:%M')}")


# ─────────────────────────────────────────────────────────────────────────────
# 5.  GAP FILTER
# ─────────────────────────────────────────────────────────────────────────────
class GapFilter:
    """
    Skips stocks that open with a significant gap from prior close.

    Why gaps are dangerous for ORB:
      • The ORB range is now built on news-driven price, not fair value
      • Initial gap move is often a manipulation — institutions gap up to
        distribute to retail buyers chasing the news
      • False breakout rate after large gaps is dramatically higher
      • Mean reversion after gap + ORB breakout is a well-documented pattern

    Parameters
    ----------
    max_gap_pct : 1.5 → skip if today's open is >1.5% away from prior close
    """

    def __init__(self, max_gap_pct: float = 1.5):
        self.max_gap_pct = max_gap_pct

    def check(self, today_open: float, prev_close: float) -> FilterResult:
        if prev_close <= 0:
            return FilterResult(True, "GapFilter", "no prior close — filter bypassed")

        gap_pct = abs(today_open - prev_close) / prev_close * 100

        if gap_pct > self.max_gap_pct:
            direction = "up" if today_open > prev_close else "down"
            return FilterResult(
                False, "GapFilter",
                f"gap {direction} {gap_pct:.1f}% — news-event open, ORB unreliable",
                value=gap_pct, threshold=self.max_gap_pct,
            )

        return FilterResult(True, "GapFilter",
                            f"gap {gap_pct:.1f}% — within acceptable range")


# ─────────────────────────────────────────────────────────────────────────────
# 6.  PDH / PDL PROXIMITY FILTER
# ─────────────────────────────────────────────────────────────────────────────
class PDHProximityFilter:
    """
    Blocks entries that sit too close to the prior day's High (longs)
    or prior day's Low (shorts).

    Why it matters
    --------------
    Institutions place limit-SELL orders at the prior day high (PDH) —
    it is a well-known institutional supply zone.  Buying within 0.5% of
    PDH means buying directly into that selling pressure; the trade
    frequently reverses immediately and hits your stop before any
    continuation.

    Same logic applies in reverse for PDL on short trades.

    Parameters
    ----------
    proximity_pct : 0.5  →  skip if entry is within 0.5% of the key level
    """

    def __init__(self, proximity_pct: float = 0.5):
        self.proximity_pct = proximity_pct

    def check(
        self,
        entry_px:  float,
        pdh:       float,
        pdl:       float,
        direction: str,
    ) -> FilterResult:
        if direction == "LONG" and pdh > 0:
            distance = (pdh - entry_px) / entry_px * 100
            if 0 <= distance < self.proximity_pct:
                return FilterResult(
                    False, "PDHProximity",
                    f"entry {entry_px:.2f} is {distance:.2f}% below PDH {pdh:.2f} "
                    f"— buying into institutional supply",
                    value=distance, threshold=self.proximity_pct,
                )
        elif direction == "SHORT" and pdl > 0:
            distance = (entry_px - pdl) / entry_px * 100
            if 0 <= distance < self.proximity_pct:
                return FilterResult(
                    False, "PDLProximity",
                    f"entry {entry_px:.2f} is {distance:.2f}% above PDL {pdl:.2f} "
                    f"— selling into institutional demand",
                    value=distance, threshold=self.proximity_pct,
                )
        return FilterResult(True, "PDHProximity", "clear of prior day key levels")


# ─────────────────────────────────────────────────────────────────────────────
# 6.  NO-TRADE ENVIRONMENT GATE
# ─────────────────────────────────────────────────────────────────────────────
class NoTradeEnvironment:
    """
    Day-level environment gate.  Blocks the entire FILTERED trading day when
    structural conditions make ORB edge unreliable.

    Checks (in order)
    -----------------
    1. NSE market holiday (hardcoded calendar, maintainable)
    2. RBI MPC meeting day (8 per year — extreme macro uncertainty)
    3. F&O monthly expiry  (last Thursday of month — skip whole day)
    4. NIFTY opening range (first 15-min candle) < skip_opening_range_pct
       → opening range < 0.4% means choppy, directionless open
    5. NIFTY index gap > skip_index_gap_pct
       → gap > 1.5% is news-driven — ORB range is meaningless

    Design note
    -----------
    Only the FILTERED config uses this gate. CURRENT and TIGHTENED still
    run on all days for comparison visibility.
    """

    # ── NSE trading holidays (FY2025-26 + FY2026-27) ─────────────────────────
    # Source: NSE India official holiday calendar
    NSE_HOLIDAYS: frozenset = frozenset({
        # FY 2025-26
        date(2025, 2, 26), date(2025, 3, 14), date(2025, 3, 31),
        date(2025, 4, 10), date(2025, 4, 14), date(2025, 4, 18),
        date(2025, 5,  1), date(2025, 8, 15), date(2025, 8, 27),
        date(2025, 10, 2), date(2025, 10, 24), date(2025, 11, 5),
        date(2025, 12, 25),
        # FY 2026-27
        date(2026, 1, 26), date(2026, 3,  2), date(2026, 3, 19),
        date(2026, 3, 31), date(2026, 4,  3), date(2026, 4, 10),
        date(2026, 4, 14), date(2026, 5,  1), date(2026, 8, 15),
        date(2026, 10, 2), date(2026, 11, 13), date(2026, 12, 25),
    })

    # ── RBI MPC announcement dates (rate decision day is the risk day) ───────
    # Source: RBI MPC calendar (8 meetings per financial year)
    RBI_MPC_DATES: frozenset = frozenset({
        # FY 2025-26
        date(2025, 4,  9), date(2025, 6,  6), date(2025, 8,  6),
        date(2025, 10, 8), date(2025, 12, 6), date(2026, 2,  7),
        # FY 2026-27
        date(2026, 4,  9), date(2026, 6,  6), date(2026, 8,  5),
        date(2026, 10, 7), date(2026, 12, 5), date(2027, 2,  5),
    })

    def __init__(
        self,
        skip_opening_range_pct: float = 0.40,
        skip_index_gap_pct:     float = 1.50,
    ):
        self.skip_opening_range_pct = skip_opening_range_pct
        self.skip_index_gap_pct     = skip_index_gap_pct

    # ── Individual checks ──────────────────────────────────────────────────

    def check_holiday(self, d: date) -> FilterResult:
        if d.weekday() >= 5:
            return FilterResult(
                False, "NoTradeEnv",
                f"{d.strftime('%A')} is a weekend — market closed",
            )
        if d in self.NSE_HOLIDAYS:
            return FilterResult(
                False, "NoTradeEnv",
                f"NSE holiday on {d.strftime('%d %b %Y')}",
            )
        return FilterResult(True, "NoTradeEnv", "Not a holiday")

    def check_rbi_mpc(self, d: date) -> FilterResult:
        if d in self.RBI_MPC_DATES:
            return FilterResult(
                False, "NoTradeEnv",
                f"RBI MPC announcement day {d.strftime('%d %b')} — macro uncertainty",
            )
        return FilterResult(True, "NoTradeEnv", "Not RBI MPC day")

    def check_fo_expiry(self, d: date) -> FilterResult:
        """
        Last Thursday of month is F&O expiry day.
        Skip the entire day (settlement volatility distorts ORB).
        """
        fo_expiry = self._last_thursday(d)
        if d == fo_expiry:
            return FilterResult(
                False, "NoTradeEnv",
                f"F&O monthly expiry day {d.strftime('%d %b')} — settlement volatility",
            )
        return FilterResult(True, "NoTradeEnv", "Not F&O expiry")

    def check_choppy_open(self, nifty_open_range_pct: float) -> FilterResult:
        if nifty_open_range_pct > 0 and nifty_open_range_pct < self.skip_opening_range_pct:
            return FilterResult(
                False, "NoTradeEnv",
                f"NIFTY opening range {nifty_open_range_pct:.2f}% < "
                f"{self.skip_opening_range_pct:.2f}% — choppy open",
                value=nifty_open_range_pct,
                threshold=self.skip_opening_range_pct,
            )
        return FilterResult(
            True, "NoTradeEnv",
            f"Open range OK ({nifty_open_range_pct:.2f}%)",
            value=nifty_open_range_pct,
            threshold=self.skip_opening_range_pct,
        )

    def check_index_gap(self, prev_close: float, today_open: float) -> FilterResult:
        if prev_close <= 0 or today_open <= 0:
            return FilterResult(True, "NoTradeEnv", "Gap data unavailable")
        gap_pct = abs(today_open - prev_close) / prev_close * 100
        if gap_pct >= self.skip_index_gap_pct:
            return FilterResult(
                False, "NoTradeEnv",
                f"NIFTY gap {gap_pct:.2f}% ≥ {self.skip_index_gap_pct:.2f}% — news-driven open",
                value=gap_pct,
                threshold=self.skip_index_gap_pct,
            )
        return FilterResult(
            True, "NoTradeEnv",
            f"NIFTY gap OK ({gap_pct:.2f}%)",
            value=gap_pct,
            threshold=self.skip_index_gap_pct,
        )

    # ── Master day-level check ──────────────────────────────────────────────

    def check_day(
        self,
        d:                    date,
        nifty_open_range_pct: float = 0.0,
        nifty_prev_close:     float = 0.0,
        nifty_open:           float = 0.0,
    ) -> FilterResult:
        """
        Master gate — returns the first failing check, or a pass.
        Call at the start of each trading day before running signals.
        """
        checks = [
            self.check_holiday(d),
            self.check_rbi_mpc(d),
            self.check_fo_expiry(d),
            self.check_index_gap(nifty_prev_close, nifty_open),
            # check_choppy_open removed: NIFTY ORB width ≠ individual stock quality;
            # per-stock quality is handled by BreakoutQualityFilter inside run_gate.
        ]
        for chk in checks:
            if not chk.passed:
                return chk
        return FilterResult(True, "NoTradeEnv", "Environment OK — all checks passed")

    @staticmethod
    def _last_thursday(d: date) -> date:
        """Return the last Thursday of d's month."""
        # Find the first day of next month, then go back to find last Thursday
        if d.month == 12:
            next_month_first = date(d.year + 1, 1, 1)
        else:
            next_month_first = date(d.year, d.month + 1, 1)
        last_day = next_month_first - timedelta(days=1)
        # weekday(): 0=Mon ... 3=Thu ... 6=Sun
        days_back = (last_day.weekday() - 3) % 7
        return last_day - timedelta(days=days_back)


# ─────────────────────────────────────────────────────────────────────────────
# 7.  VOLUME CONFIRMATION FILTER
# ─────────────────────────────────────────────────────────────────────────────
class VolumeConfirmationFilter:
    """
    Rejects breakout entries where the breakout candle's volume is below
    ``min_rvol`` × the historical average volume at the same candle index.

    Rationale: low-volume breakouts have no institutional participation —
    price displaced purely on retail order-flow almost always reverts.

    Parameters
    ----------
    min_rvol      : relative-volume threshold (default 0.8 × historical avg)
    lookback_days : number of prior trading days to average (default 5)
    """

    def __init__(self, min_rvol: float = 0.8, lookback_days: int = 5):
        self.min_rvol      = min_rvol
        self.lookback_days = lookback_days

    def check(
        self,
        candle:               Candle,
        candle_idx:           int,
        prior_days_candles:   list,          # list[list[Candle]], oldest first
    ) -> FilterResult:
        """
        Parameters
        ----------
        candle             : the breakout candle under review
        candle_idx         : 0-based position in the session (ORB=0, 1st break=1, …)
        prior_days_candles : historical session candle lists for this symbol
        """
        if not prior_days_candles:
            return FilterResult(True, "VolumeConfirm", "No history — allowing")

        hist_vols = [
            day[candle_idx].volume
            for day in prior_days_candles[-self.lookback_days:]
            if candle_idx < len(day) and day[candle_idx].volume > 0
        ]
        if len(hist_vols) < 2:
            return FilterResult(True, "VolumeConfirm", "Insufficient history — allowing")

        avg_vol = sum(hist_vols) / len(hist_vols)
        if avg_vol <= 0:
            return FilterResult(True, "VolumeConfirm", "Zero historical volume — allowing")

        rvol = candle.volume / avg_vol
        if rvol < self.min_rvol:
            return FilterResult(
                False, "VolumeConfirm",
                f"RVOL {rvol:.2f}× < {self.min_rvol:.1f}× min "
                f"(vol {candle.volume:,} vs avg {avg_vol:,.0f})",
                value=rvol, threshold=self.min_rvol,
            )
        return FilterResult(
            True, "VolumeConfirm",
            f"RVOL OK {rvol:.2f}× (vol {candle.volume:,} vs avg {avg_vol:,.0f})",
            value=rvol, threshold=self.min_rvol,
        )


# ─────────────────────────────────────────────────────────────────────────────
# 8.  ORB VOLUME GATE  (first-candle institutional participation)
# ─────────────────────────────────────────────────────────────────────────────
class OrbVolumeGate:
    """
    Rejects the entire ORB setup if the opening 15-min candle's volume is
    below the ``min_percentile`` of historical first-candle volumes.

    A thin first candle means the ORB range was carved out by retail noise;
    institutional orders have not yet established positions, so any breakout
    from that range is prone to failure.

    Parameters
    ----------
    min_percentile : first-candle volume must reach this percentile of the
                     last ``lookback_days`` first-candle volumes (default 0.40)
    lookback_days  : number of prior trading days to use (default 20)
    """

    def __init__(self, min_percentile: float = 0.40, lookback_days: int = 20):
        self.min_percentile = min_percentile
        self.lookback_days  = lookback_days

    def check(
        self,
        orb_volume:          int,
        prior_days_candles:  list,           # list[list[Candle]], oldest first
    ) -> FilterResult:
        if not prior_days_candles:
            return FilterResult(True, "OrbVolumeGate", "No history — allowing")

        first_vols = sorted(
            day[0].volume
            for day in prior_days_candles[-self.lookback_days:]
            if day and day[0].volume > 0
        )
        if len(first_vols) < 5:
            return FilterResult(True, "OrbVolumeGate", "Insufficient history — allowing")

        cut = int(self.min_percentile * len(first_vols))
        threshold = first_vols[max(0, cut - 1)]

        if orb_volume < threshold:
            return FilterResult(
                False, "OrbVolumeGate",
                f"ORB vol {orb_volume:,} < {self.min_percentile:.0%}tile "
                f"({threshold:,}) — thin open, no institutional participation",
                value=orb_volume, threshold=threshold,
            )
        return FilterResult(
            True, "OrbVolumeGate",
            f"ORB vol OK {orb_volume:,} >= {threshold:,}",
            value=orb_volume, threshold=threshold,
        )
