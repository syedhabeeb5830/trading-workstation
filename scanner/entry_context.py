"""
scanner/entry_context.py — Entry Context Engine (Improvement 1)
===============================================================
Phase-8 evidence showed actionability adds no edge: ACTION_NOW didn't beat AVOID.
Root cause — entry quality was judged almost entirely by "distance from breakout",
so a textbook BUY-THE-DIP in a strong uptrend (price below the recent high) scored
poorly and got dumped into AVOID, while extended names that happened to sit near a
high scored well.

This engine reads the SETUP TYPE from price structure, so entry quality can be
judged in context:

    PULLBACK_SETUP     uptrend, price pulled back to a rising EMA20/support  → prime dip-buy
    BREAKOUT_SETUP     price at/just through the recent base high            → fresh trigger
    TREND_CONTINUATION strong uptrend, riding extended above EMA20           → momentum (stretched)
    BASE_BUILDING      tight sideways range, flat EMAs                       → not triggered, watch
    NO_SETUP           downtrend / no constructive structure                → avoid

It is a pure classifier (OHLCV in, context + levels out) — it knows nothing about
scores, RR, or any other engine. The actionability scorers consume its output.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

# Tunables (percent unless noted)
_NEAR_HIGH_PCT     = 2.5     # within this % of the 20-day high → "at the breakout"
_PULLBACK_MIN_OFF  = 2.5     # at least this far below the high to count as a pullback
_EMA20_NEAR_LO     = -4.0    # price this far below..             (pullback zone vs EMA20)
_EMA20_NEAR_HI     = 6.0     #            ..this far above EMA20
_CONTINUATION_EXT  = 6.0     # EMA20 extension beyond this in a strong trend → continuation
_BASE_RANGE_PCT    = 12.0    # 20-day range tighter than this → base
_BASE_FLAT_PCT     = 2.0     # |EMA20-EMA50|/EMA50 below this → flat EMAs


@dataclass
class EntryContext:
    context:          str
    reason:           str
    price:            float
    ema20:            float
    ema50:            float
    h20:              float       # 20-day high (the breakout reference)
    ll20:             float       # 20-day low (base floor)
    ema20_ext:        float       # % price vs EMA20 (+ = above)
    dist_below_high:  float       # % below the 20-day high (+ = below)
    range20_pct:      float       # 20-day high-low range as % of mean
    trend_up:         bool
    extension_pct:    float       # how stretched: max(above-breakout, EMA20 extension)


class EntryContextClassifier:
    """Classifies the current price structure into a swing setup type."""

    def classify(self, df: pd.DataFrame) -> EntryContext:
        close = df["Close"]
        price = float(close.iloc[-1])
        ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        ema50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        h20 = float(df["High"].tail(20).max())
        ll20 = float(df["Low"].tail(20).min())
        mean20 = float(close.tail(20).mean()) or price

        ema20_ext = (price - ema20) / ema20 * 100 if ema20 else 0.0
        dist_below_high = (h20 - price) / h20 * 100 if h20 else 0.0
        range20_pct = (h20 - ll20) / mean20 * 100 if mean20 else 999.0
        trend_up = ema20 > ema50
        strong_up = price > ema20 > ema50
        flat_emas = abs(ema20 - ema50) / ema50 * 100 < _BASE_FLAT_PCT if ema50 else False
        above_breakout = max(0.0, (price - h20) / h20 * 100) if h20 else 0.0
        extension_pct = round(max(above_breakout, ema20_ext), 2)

        # ── Decision tree (ordered) ──────────────────────────────────────────
        if price < ema50 and ema20 < ema50:
            ctx, why = "NO_SETUP", "Downtrend (price < EMA50, EMA20 < EMA50)"
        elif range20_pct <= _BASE_RANGE_PCT and flat_emas and dist_below_high > _NEAR_HIGH_PCT:
            ctx, why = "BASE_BUILDING", f"Tight base ({range20_pct:.0f}% range), flat EMAs"
        elif dist_below_high <= _NEAR_HIGH_PCT:
            ctx, why = "BREAKOUT_SETUP", f"At 20D high ({dist_below_high:+.1f}% from breakout)"
        elif trend_up and _EMA20_NEAR_LO <= ema20_ext <= _EMA20_NEAR_HI \
                and dist_below_high > _PULLBACK_MIN_OFF:
            ctx, why = "PULLBACK_SETUP", (f"Pullback to EMA20 ({ema20_ext:+.1f}%), "
                                          f"{dist_below_high:.1f}% off high")
        elif strong_up and ema20_ext > _CONTINUATION_EXT:
            ctx, why = "TREND_CONTINUATION", f"Riding trend, {ema20_ext:+.1f}% above EMA20"
        elif trend_up:
            ctx, why = "PULLBACK_SETUP", f"Uptrend, mild pullback ({ema20_ext:+.1f}% vs EMA20)"
        else:
            ctx, why = "NO_SETUP", "No constructive structure"

        return EntryContext(ctx, why, round(price, 2), round(ema20, 2), round(ema50, 2),
                            round(h20, 2), round(ll20, 2), round(ema20_ext, 2),
                            round(dist_below_high, 2), round(range20_pct, 2),
                            trend_up, extension_pct)
