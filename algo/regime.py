"""
algo/regime.py — Intraday Market Regime Classifier
====================================================
At 10:00 (after the 3rd 15-min candle closes at 09:45) classifies the NIFTY50
regime for the remainder of the trading session.

Regime table
------------
TREND_UP     ≥ 2/3 candles bullish + cumulative range ≥ MIN_RANGE_PCT + last close ≥ midpoint
TREND_DOWN   ≥ 2/3 candles bearish + cumulative range ≥ MIN_RANGE_PCT + last close ≤ midpoint
CHOP         mixed closes OR narrow range
UNKNOWN      insufficient candles (< 3)

Impact on ORB exits (AdaptiveExit multipliers)
----------------------------------------------
              T1 mult    T2 mult    runner?
TREND_UP      0.80×      3.00×      YES — exit half at T1, let runner go to T2
TREND_DOWN    0.80×      3.00×      YES — same, mirrored for shorts
CHOP          0.60×       —         NO  — exit ALL at T1, no runner (fees kill drifters)
UNKNOWN       1.00×      2.50×      YES — default (Phase 1 baseline)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from algo.candles import Candle


# ─────────────────────────────────────────────────────────────────────────────
# TYPES
# ─────────────────────────────────────────────────────────────────────────────

class MarketRegime(Enum):
    TREND_UP   = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    CHOP       = "CHOP"
    UNKNOWN    = "UNKNOWN"


# Exit multipliers per regime — used directly by backtest._replay_day
REGIME_EXIT_PARAMS: dict[str, dict] = {
    MarketRegime.TREND_UP.value:   {"t1_mult": 0.80, "t2_mult": 3.00, "has_runner": True},
    MarketRegime.TREND_DOWN.value: {"t1_mult": 0.80, "t2_mult": 3.00, "has_runner": True},
    MarketRegime.CHOP.value:       {"t1_mult": 0.60, "t2_mult": 2.50, "has_runner": False},
    MarketRegime.UNKNOWN.value:    {"t1_mult": 1.00, "t2_mult": 2.50, "has_runner": True},
}


@dataclass
class RegimeResult:
    regime:          MarketRegime
    reason:          str
    nifty_range_pct: float = 0.0
    candles_used:    int   = 0
    # Filled automatically from REGIME_EXIT_PARAMS
    t1_mult:         float = 1.00
    t2_mult:         float = 2.50
    has_runner:      bool  = True


# ─────────────────────────────────────────────────────────────────────────────
# CLASSIFIER
# ─────────────────────────────────────────────────────────────────────────────

class RegimeClassifier:
    """
    Classify the intraday NIFTY regime from the first 5 completed 15-min candles
    (09:15–10:15).  Using 5 candles (vs 3) filters out the noisy institutional
    order-filling in candles 1–2 and gives a more reliable trend signal.

    Parameters
    ----------
    min_range_pct   : minimum 5-candle cumulative range to call a trend (default 0.30%)
    trend_threshold : minimum same-direction candles out of 5 to call a trend (default 3)
    """

    def __init__(self, min_range_pct: float = 0.30, trend_threshold: int = 3):
        self.min_range_pct   = min_range_pct
        self.trend_threshold = trend_threshold

    def classify(self, nifty_candles: list[Candle]) -> RegimeResult:
        """
        Pass the first 5+ completed 15-min NIFTY candles (09:15–10:15).
        Returns a RegimeResult with regime and pre-computed exit params.
        Falls back to UNKNOWN if fewer than 5 candles are available.
        """
        if len(nifty_candles) < 5:
            result = RegimeResult(
                MarketRegime.UNKNOWN,
                f"Only {len(nifty_candles)}/5 candles available",
            )
            self._set_params(result)
            return result

        c1, c2, c3, c4, c5 = nifty_candles[:5]

        # Direction of each candle (close vs open)
        directions = [
            1  if c.close > c.open else
            -1 if c.close < c.open else 0
            for c in (c1, c2, c3, c4, c5)
        ]
        up_count   = sum(1 for d in directions if d > 0)
        down_count = sum(1 for d in directions if d < 0)

        # Cumulative range across all 5 candles as % of first candle's open
        total_high = max(c.high for c in (c1, c2, c3, c4, c5))
        total_low  = min(c.low  for c in (c1, c2, c3, c4, c5))
        ref        = c1.open if c1.open else c1.close
        range_pct  = (total_high - total_low) / ref * 100 if ref > 0 else 0.0

        # Momentum confirmation: close location of the 5th candle (10:15)
        span5 = c5.high - c5.low
        cl5   = (c5.close - c5.low) / span5 if span5 > 0 else 0.5

        if (up_count >= self.trend_threshold
                and range_pct >= self.min_range_pct
                and cl5 >= 0.50):
            result = RegimeResult(
                MarketRegime.TREND_UP,
                f"{up_count}/5 bullish · range {range_pct:.2f}% · c5_loc {cl5:.0%}",
                range_pct, 5,
            )
        elif (down_count >= self.trend_threshold
                and range_pct >= self.min_range_pct
                and cl5 <= 0.50):
            result = RegimeResult(
                MarketRegime.TREND_DOWN,
                f"{down_count}/5 bearish · range {range_pct:.2f}% · c5_loc {cl5:.0%}",
                range_pct, 5,
            )
        else:
            result = RegimeResult(
                MarketRegime.CHOP,
                f"Mixed: up={up_count} dn={down_count} · range {range_pct:.2f}%",
                range_pct, 5,
            )

        self._set_params(result)
        return result

    @staticmethod
    def _set_params(result: RegimeResult) -> None:
        """Populate t1_mult / t2_mult / has_runner from the lookup table."""
        params = REGIME_EXIT_PARAMS[result.regime.value]
        result.t1_mult    = params["t1_mult"]
        result.t2_mult    = params["t2_mult"]
        result.has_runner = params["has_runner"]
