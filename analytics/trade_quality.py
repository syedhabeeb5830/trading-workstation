"""
analytics/trade_quality.py — Trade Quality Intelligence Layer (Phase O)
======================================================================
A professional RISK-MANAGER overlay. It does NOT predict markets and adds NO new
indicators (no RSI/MACD/sentiment/forecasts). It reads only price/volume STRUCTURE
from the OHLCV already loaded by the screen, plus the project's own already-validated
analytics (path-analog reach-probabilities, 5y move distribution, earnings calendar,
regime/sector/RS context), and answers ONE question per candidate:

    "Is this a high-quality place to risk capital — or should we pass?"

The objective is to SURFACE and PRICE risk, not to hide trades. The trader makes the
final call — so every genuine candidate stays VISIBLE, is GRADED, and is SIZED DOWN as
quality falls (lower grade / more risk ⇒ smaller suggested size, floored so it stays
actionable). Better to see some stocks with their risk on a choppy day than nothing.
Eight assessments combine into a single TRADE QUALITY SCORE + risk tier:

    1. Stop quality      — fragile / crowded / swept stop placement
    2. Entry quality     — chasing / gap / exhaustion / resistance / FOMO
    3. Target quality    — analog-grounded T1/T2 with real reach probabilities
    4. Death-zone risk   — contexts where good-looking trades fail
    5. Liquidity-trap    — weak-participation / failed-breakout / exhaustion traps
    6. Structure quality — base / contraction / trend-cleanliness / support integrity
    7. Management plan    — invalidation / scale-out / trail / continuation (with WHY)
    8. Final score+grade  — A (80+) / B (70-79) / C (60-69) / D (<60), tier PRIME/CAUTION/
                            HIGH_RISK. PRIME = take with confidence; CAUTION/HIGH_RISK shown
                            with louder flags + smaller size — your call, not auto-rejected.

NOTE ON STATUS: these are STRUCTURAL RISK HEURISTICS, not OOS-validated alpha. They
are wired as a capital-preservation GATE/SIZER/annotator on top of the frozen
selection engines — they never recompute, rank, or inflate any existing score. The
only validated numbers used are the analog probabilities and the 5y distribution
(both already in the system); everything else is transparent risk geometry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from scanner.scanner import compute_atr_series


# ═════════════════════════════════════════════════════════════════════════════
# STRUCTURE PRIMITIVES (pure price/volume geometry — no indicators)
# ═════════════════════════════════════════════════════════════════════════════
def _pivots(values: np.ndarray, left: int = 3, right: int = 3,
            kind: str = "low") -> list[tuple[int, float]]:
    """Fractal swing points: a low (high) is a local min (max) over [i-left, i+right]."""
    out: list[tuple[int, float]] = []
    n = len(values)
    for i in range(left, n - right):
        w = values[i - left: i + right + 1]
        v = values[i]
        if kind == "low" and v == w.min():
            out.append((i, float(v)))
        elif kind == "high" and v == w.max():
            out.append((i, float(v)))
    return out


def _round_step(price: float) -> float:
    """The price increment retail stops cluster on, scaled to the name's magnitude."""
    if price < 50:    return 1.0
    if price < 200:   return 5.0
    if price < 500:   return 10.0
    if price < 1000:  return 25.0
    if price < 5000:  return 50.0
    return 100.0


def _nearest_round(price: float) -> float:
    step = _round_step(price)
    return round(price / step) * step


def _nearest_support_below(df: pd.DataFrame, entry: float, lookback: int = 80) -> Optional[float]:
    """Highest swing-low that still sits below the entry — the level price would
    test first on a pullback (where protective stops naturally cluster)."""
    lows = df["Low"].tail(lookback).values
    piv = _pivots(lows, kind="low")
    below = [p for _, p in piv if p < entry * 0.999]
    if below:
        return max(below)                       # nearest support under price
    swing = float(df["Low"].tail(10).min())
    return swing if swing < entry else None


def _nearest_overhead(df: pd.DataFrame, entry: float, lookback: int = 160) -> Optional[float]:
    """Nearest swing-high above entry — the first overhead supply wall."""
    highs = df["High"].tail(lookback).values
    piv = _pivots(highs, kind="high")
    above = [p for _, p in piv if p > entry * 1.002]
    if above:
        return min(above)
    hi = float(df["High"].tail(lookback).max())
    return hi if hi > entry * 1.002 else None


def _consec_expansion(df: pd.DataFrame, n: int = 6) -> int:
    """Count of trailing consecutive UP candles each with a wider range than the one
    before it — a volatility-expansion blow-off pattern (late, chase-prone)."""
    o, h, l, c = (df["Open"].values, df["High"].values,
                  df["Low"].values, df["Close"].values)
    cnt = 0
    for i in range(len(c) - 1, max(len(c) - 1 - n, 0), -1):
        rng, prng = h[i] - l[i], h[i - 1] - l[i - 1]
        if c[i] > o[i] and rng > prng:
            cnt += 1
        else:
            break
    return cnt


def _max_gap_pct(df: pd.DataFrame, n: int = 3) -> float:
    """Largest up-gap (open vs prior close) over the last n sessions, in %."""
    o, c = df["Open"].values, df["Close"].values
    g = 0.0
    for i in range(len(c) - 1, max(len(c) - n, 1) - 1, -1):
        if c[i - 1] > 0:
            g = max(g, (o[i] - c[i - 1]) / c[i - 1] * 100)
    return round(g, 2)


def _atr_contraction(df: pd.DataFrame, atr_now: float) -> float:
    """ratio = ATR_now / ATR_~20-sessions-ago. <1 ⇒ volatility is contracting
    (constructive coil); >1 ⇒ expanding (late/risky)."""
    s = compute_atr_series(df)
    if len(s) < 25:
        return 1.0
    past = float(s.iloc[-21])
    return round(atr_now / past, 2) if past and past > 0 else 1.0


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _trend_cleanliness(df: pd.DataFrame, win: int = 50) -> tuple[float, int]:
    """How cleanly price respects its EMA20: (% of last `win` closes above EMA20,
    number of EMA20 crosses). Clean = high % above + few crosses (low whipsaw)."""
    c = df["Close"]
    ema = _ema(c, 20)
    tail_c = c.tail(win).values
    tail_e = ema.tail(win).values
    above = tail_c >= tail_e
    pct_above = float(above.mean()) * 100
    crosses = int(np.sum(above[1:] != above[:-1]))
    return round(pct_above, 0), crosses


def _higher_lows(df: pd.DataFrame, lookback: int = 60) -> bool:
    """Are the most recent swing lows ascending? (support integrity)."""
    lows = df["Low"].tail(lookback).values
    piv = [p for _, p in _pivots(lows, kind="low")]
    if len(piv) < 2:
        return False
    return piv[-1] > piv[-2]


def _vol_stats(df: pd.DataFrame, vol_ctx: Optional[dict]) -> dict:
    """Prefer the screen's partial-candle-safe vol_ctx; else compute a basic fallback."""
    if vol_ctx:
        return vol_ctx
    v = df["Volume"]
    if len(v) < 22:
        return {}
    v20 = float(v.iloc[-21:-1].mean())
    if v20 <= 0:
        return {}
    v3 = float(v.iloc[-4:-1].mean())
    h20 = float(df["High"].iloc[-20:].max())
    cur = float(df["Close"].iloc[-1])
    return {"rvol": round(float(v.iloc[-1]) / v20, 2),
            "vol_trend_3d": round(v3 / v20, 2),
            "pullback_pct": round((h20 - cur) / h20 * 100, 1) if h20 else 0.0,
            "above_ema20": cur >= float(_ema(df["Close"], 20).iloc[-1]) * 0.97}


def _failed_breakout_attempts(df: pd.DataFrame, lookback: int = 70) -> int:
    """Repeated rejection at the SAME resistance shelf = a supply wall. Count the
    distinct swing-highs that sit within 1.5% of the window's highest swing-high
    (minus the shelf itself). A name making clean higher-highs spreads its swing
    highs out → ~0; a double/triple-top clusters them → 1-2+."""
    highs = df["High"].tail(lookback).values
    piv = [p for _, p in _pivots(highs, kind="high")]
    if len(piv) < 2:
        return 0
    top = max(piv)
    near = [p for p in piv if p >= top * 0.985]
    return max(0, len(near) - 1)


# ═════════════════════════════════════════════════════════════════════════════
# RESULT TYPES
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class StopQuality:
    score: float
    label: str                      # GOOD_STOP / ACCEPTABLE_STOP / CROWDED_STOP / DANGEROUS_STOP
    reason: str
    suggested_stop: Optional[float] = None
    size_factor: float = 1.0


@dataclass
class EntryQuality:
    score: float
    label: str                      # IDEAL_ENTRY / GOOD_ENTRY / LATE_ENTRY / FOMO_ENTRY
    reasons: list[str] = field(default_factory=list)
    reject: bool = False


@dataclass
class TargetQuality:
    t1: float
    t2: float
    p_t1: Optional[float]
    p_t2: Optional[float]
    realism: float
    reason: str
    capped: bool = False


@dataclass
class RiskFlag:
    level: str                      # LOW / MEDIUM / HIGH
    factors: list[str] = field(default_factory=list)
    size_factor: float = 1.0


@dataclass
class StructureQuality:
    score: float
    label: str                      # ELITE / GOOD / AVERAGE / POOR
    reasons: list[str] = field(default_factory=list)


@dataclass
class ManagementPlan:
    invalidation: float
    scale_out: float
    trail_method: str
    trend_continuation: str
    why: list[str] = field(default_factory=list)


@dataclass
class TradeQuality:
    final_score: float
    grade: str                      # A / B / C / D  (D = lowest, still shown)
    risk_tier: str                  # PRIME / CAUTION / HIGH_RISK (display + sizing, never hides)
    recommended: bool               # PRIME setups the system would take with confidence
    stop: float                     # final (possibly improved) stop
    size_factor: float              # multiply position size by this (floored, always actionable)
    structure: StructureQuality
    entry: EntryQuality
    stop_q: StopQuality
    target: TargetQuality
    death_zone: RiskFlag
    liquidity_trap: RiskFlag
    plan: ManagementPlan
    headline: list[str] = field(default_factory=list)


# ═════════════════════════════════════════════════════════════════════════════
# 1) STOP QUALITY
# ═════════════════════════════════════════════════════════════════════════════
def assess_stop(df: pd.DataFrame, entry: float, stop: float, atr: float) -> StopQuality:
    if not (entry and stop and atr and atr > 0) or stop >= entry:
        return StopQuality(0, "DANGEROUS_STOP", "no valid stop geometry", None, 0.0)

    risk_atr = (entry - stop) / atr
    support = _nearest_support_below(df, entry)
    rnd = _nearest_round(stop)
    sweep = 0.35 * atr                       # how far a stop-hunt typically pokes
    score = 100.0
    bits: list[str] = []
    suggested: Optional[float] = None

    # a) inside ATR noise → gets knocked out on a normal wiggle
    if risk_atr < 1.0:
        score -= 40; bits.append(f"only {risk_atr:.1f}×ATR — inside daily noise")
        suggested = round(entry - 1.6 * atr, 2)
    elif risk_atr < 1.5:
        score -= 12; bits.append(f"{risk_atr:.1f}×ATR — a touch tight")
    elif risk_atr > 5.0:
        score -= 10; bits.append(f"{risk_atr:.1f}×ATR — wide, sizing will be small")

    # b) relation to the obvious swing low (where the herd's stops sit). With
    #    d = (stop - support): d<0 ⇒ stop BELOW support (room); d>0 ⇒ stop ABOVE it
    #    so a routine test of that support takes you out before it even holds.
    safer = round(support - max(0.6 * atr, 0.005 * support), 2) if support else None
    if support:
        d_atr = (stop - support) / atr
        if d_atr <= -0.15:
            bits.append("below major structure")
        elif d_atr <= 0.35:                   # sitting in the stop-cluster on the shelf
            score -= 28; bits.append("sits on the obvious swing low (stop-cluster sweep zone)")
            suggested = safer
        else:                                 # stop ABOVE support ⇒ a support test stops you first
            score -= 45; bits.append("above the swing low — a normal support test stops you out")
            suggested = safer

    # c) round-number magnet (stops pile up hugging round figures → swept). Tight
    #    band so it only fires when the stop genuinely sits on the round number.
    rband = min(0.2 * atr, 0.004 * stop)
    if abs(stop - rnd) <= rband:
        score -= 8; bits.append(f"hugging round number ₹{rnd:,.0f}")
        if suggested is None and support is None:
            suggested = round(rnd - max(0.5 * atr, 0.006 * stop), 2)

    score = max(0.0, min(100.0, round(score, 0)))
    if score >= 80:
        label, sf = "GOOD_STOP", 1.0
    elif score >= 65:
        label, sf = "ACCEPTABLE_STOP", 1.0
    elif score >= 50:
        label, sf = "CROWDED_STOP", 0.6
    else:
        label, sf = "DANGEROUS_STOP", 0.4    # size down hard + offer a safer stop, never hide

    # only surface a suggestion that is actually safer (lower) than the current stop
    if suggested is not None and suggested >= stop:
        suggested = None
    reason = "; ".join(bits) if bits else "outside volatility envelope and below structure"
    return StopQuality(score, label, reason, suggested, sf)


# ═════════════════════════════════════════════════════════════════════════════
# 2) ENTRY QUALITY
# ═════════════════════════════════════════════════════════════════════════════
def assess_entry(df: pd.DataFrame, entry: float, atr: float, *, entry_context: str,
                 extension_pct: float, vol_ctx: Optional[dict],
                 overhead: Optional[float], risk_per_share: float) -> EntryQuality:
    score = 70.0
    reasons: list[str] = []
    vc = _vol_stats(df, vol_ctx)

    # ── penalties ────────────────────────────────────────────────────────────
    if extension_pct > 5:
        pen = min(35, (extension_pct - 5) * 4)
        score -= pen; reasons.append(f"chasing {extension_pct:.0f}% above support")
    gap = _max_gap_pct(df, 3)
    if gap > 3:
        score -= min(20, gap * 3); reasons.append(f"recent gap-up {gap:.0f}% (chase risk)")
    exp = _consec_expansion(df)
    if exp >= 3:
        score -= 8 + (exp - 3) * 5; reasons.append(f"{exp} expansion candles — climactic")
    if overhead and entry:
        head_atr = (overhead - entry) / atr if atr else 9
        if 0 < head_atr < 1.5:
            score -= 18; reasons.append(f"resistance ₹{overhead:,.0f} just {head_atr:.1f}×ATR overhead")
        if risk_per_share and (overhead - entry) < risk_per_share:
            score -= 12; reasons.append("reward-to-risk compressed by overhead supply")

    # ── rewards ──────────────────────────────────────────────────────────────
    if entry_context == "PULLBACK_SETUP":
        score += 18; reasons.append("pullback into support")
    if _atr_contraction(df, atr) < 0.9:
        score += 10; reasons.append("volatility contraction")
    if vc.get("vol_trend_3d", 1.0) <= 0.85 and vc.get("pullback_pct", 0) > 0:
        score += 8; reasons.append("low-volume retracement")
    if entry_context == "BREAKOUT_SETUP":
        rng20 = (float(df["High"].tail(20).max()) - float(df["Low"].tail(20).min()))
        base_pct = rng20 / entry * 100 if entry else 99
        if base_pct <= 14:
            score += 12; reasons.append("fresh breakout from a tight base")

    score = max(0.0, min(100.0, round(score, 0)))
    if score >= 80:
        label, rej = "IDEAL_ENTRY", False
    elif score >= 65:
        label, rej = "GOOD_ENTRY", False
    elif score >= 50:
        label, rej = "LATE_ENTRY", False
    else:
        label, rej = "FOMO_ENTRY", True
    return EntryQuality(score, label, reasons, rej)


# ═════════════════════════════════════════════════════════════════════════════
# 3) TARGET QUALITY (analog-grounded, never arbitrary R)
# ═════════════════════════════════════════════════════════════════════════════
def _reach_prob(move_pct: float, cohort: Optional[dict],
                expected: Optional[dict]) -> Optional[float]:
    """Probability the move reaches `move_pct` (before stop), from the path-analog
    reach curve (p10/p15/p20). Falls back to the 5y move distribution. Monotone."""
    if cohort and all(cohort.get(k) is not None for k in ("p10", "p15", "p20")):
        xs = [10.0, 15.0, 20.0]
        ys = [cohort["p10"] / 100, cohort["p15"] / 100, cohort["p20"] / 100]
        win = cohort.get("win_pct")
        if move_pct <= xs[0]:
            lo = (win / 100 if win else min(0.95, ys[0] + 0.15))
            return round(min(0.97, lo - (lo - ys[0]) * (move_pct / xs[0])), 2) if move_pct > 0 else lo
        if move_pct >= xs[-1]:
            return round(max(0.02, ys[-1] * (xs[-1] / move_pct)), 2)
        return round(float(np.interp(move_pct, xs, ys)), 2)
    if expected:                              # distribution fallback
        med, p75 = expected.get("med20_pct"), expected.get("p75_20_pct")
        p90 = expected.get("p90_20_pct", p75)
        if med is not None and p75 is not None:
            pts_x = [med, p75, p90 if p90 else p75 + 1]
            pts_y = [0.50, 0.25, 0.10]
            if move_pct <= pts_x[0]:
                return round(min(0.85, 0.50 + (pts_x[0] - move_pct) / max(pts_x[0], 1) * 0.3), 2)
            return round(float(np.interp(move_pct, pts_x, pts_y)), 2)
    return None


def assess_targets(entry: float, t1: float, t2: float, *, overhead: Optional[float],
                   cohort: Optional[dict], expected: Optional[dict],
                   calib_p1: Optional[float] = None,
                   calib_p2: Optional[float] = None) -> TargetQuality:
    m1 = (t1 / entry - 1) * 100 if (t1 and entry) else 0
    m2 = (t2 / entry - 1) * 100 if (t2 and entry) else 0
    # Prefer the OOS-validated calibrated fill rate (itself derived from the real
    # analog excursion distribution); fall back to the cohort reach-curve / 5y dist.
    p1 = calib_p1 if calib_p1 is not None else _reach_prob(m1, cohort, expected)
    p2 = calib_p2 if calib_p2 is not None else _reach_prob(m2, cohort, expected)

    bits: list[str] = []
    realism = 100.0
    capped = False
    # probability realism
    if p1 is not None:
        if p1 < 0.20:
            realism -= 45; bits.append(f"T1 reach only {p1*100:.0f}% — analogs rarely get there")
        elif p1 < 0.40:
            realism -= 15; bits.append(f"T1 reach {p1*100:.0f}% — modest")
        else:
            bits.append(f"T1 reach {p1*100:.0f}% — bankable")
    if p2 is not None and p2 < 0.15:
        realism -= 15; capped = True
        bits.append(f"T2 reach {p2*100:.0f}% — runner only, do not count on it")
    # overhead supply between entry and a target lowers realism
    if overhead and t1 and overhead < t1:
        realism -= 18; bits.append(f"overhead supply ₹{overhead:,.0f} sits under T1")
    realism = max(0.0, min(100.0, round(realism, 0)))
    return TargetQuality(t1, t2, p1, p2, realism, "; ".join(bits), capped)


# ═════════════════════════════════════════════════════════════════════════════
# 4) DEATH-ZONE DETECTOR
# ═════════════════════════════════════════════════════════════════════════════
def detect_death_zone(df: pd.DataFrame, *, entry: float, atr: float,
                      entry_context: str, extension_pct: float, vol_ctx: Optional[dict],
                      overhead: Optional[float], sector_grade: Optional[str],
                      breadth: float, participation: float,
                      earnings_days: Optional[int]) -> RiskFlag:
    vc = _vol_stats(df, vol_ctx)
    f: list[str] = []
    if entry_context == "BREAKOUT_SETUP" and overhead and atr:
        if 0 < (overhead - entry) / atr < 2.0:
            f.append("breakout straight into overhead supply")
    if entry_context == "BREAKOUT_SETUP" and vc.get("rvol", 1.0) < 1.0:
        f.append(f"breakout on weak volume ({vc.get('rvol')}×)")
    runup = (entry / float(df["Low"].tail(10).min()) - 1) * 100 if entry else 0
    if extension_pct > 8 or runup > 18:
        f.append(f"already extended ({extension_pct:.0f}% ext, +{runup:.0f}%/10d)")
    if sector_grade and sector_grade in ("C", "C-", "D", "D+", "F"):
        f.append(f"sector weakening (grade {sector_grade}) while stock holds up")
    if participation < 45 or breadth < 45:
        f.append(f"broad participation deteriorating (breadth {breadth:.0f}/part {participation:.0f})")
    if earnings_days is not None and 0 <= earnings_days <= 7:
        f.append(f"earnings in {earnings_days}d — event/gap risk")

    level = "LOW" if len(f) <= 1 else ("MEDIUM" if len(f) == 2 else "HIGH")
    sf = {"LOW": 1.0, "MEDIUM": 0.75, "HIGH": 0.5}[level]   # HIGH → size down hard, not hidden
    return RiskFlag(level, f, sf)


# ═════════════════════════════════════════════════════════════════════════════
# 5) LIQUIDITY-TRAP DETECTOR
# ═════════════════════════════════════════════════════════════════════════════
def detect_liquidity_trap(df: pd.DataFrame, *, entry_context: str,
                          vol_ctx: Optional[dict], rs_status: str,
                          leadership: float) -> RiskFlag:
    vc = _vol_stats(df, vol_ctx)
    f: list[str] = []
    if entry_context == "BREAKOUT_SETUP" and vc.get("vol_trend_3d", 1.0) < 0.9:
        f.append("breakout on declining volume")
    attempts = _failed_breakout_attempts(df)
    if attempts >= 2:
        f.append(f"{attempts} prior rejections at this resistance shelf — supply overhead")
    # abnormal intraday exhaustion: long upper wick on heavy volume (last complete bar)
    last = df.iloc[-1]
    rng = float(last["High"] - last["Low"])
    if rng > 0:
        upper_wick = (float(last["High"]) - float(last["Close"])) / rng
        if upper_wick > 0.55 and vc.get("rvol", 0) >= 1.5:
            f.append("upper-wick exhaustion on heavy volume")
    if rs_status in ("NEUTRAL", "LAGGARD"):
        f.append(f"weak relative participation ({rs_status.lower()})")
    if leadership < 45:
        f.append(f"narrow market leadership ({leadership:.0f}/100)")

    level = "LOW" if len(f) <= 1 else ("MEDIUM" if len(f) == 2 else "HIGH")
    sf = {"LOW": 1.0, "MEDIUM": 0.7, "HIGH": 0.5}[level]   # trap → smaller size, not auto-reject
    return RiskFlag(level, f, sf)


# ═════════════════════════════════════════════════════════════════════════════
# 6) STRUCTURE QUALITY
# ═════════════════════════════════════════════════════════════════════════════
def assess_structure(df: pd.DataFrame, entry: float, atr: float) -> StructureQuality:
    score = 50.0
    bits: list[str] = []

    rng20 = float(df["High"].tail(20).max()) - float(df["Low"].tail(20).min())
    base_pct = rng20 / entry * 100 if entry else 99
    if base_pct <= 9:
        score += 18; bits.append(f"tight base ({base_pct:.0f}% range)")
    elif base_pct <= 14:
        score += 8; bits.append(f"orderly base ({base_pct:.0f}% range)")
    else:
        score -= 8; bits.append(f"loose/wide structure ({base_pct:.0f}% range)")

    contr = _atr_contraction(df, atr)
    if contr < 0.85:
        score += 16; bits.append("volatility contracting")
    elif contr > 1.25:
        score -= 12; bits.append("volatility expanding")

    pct_above, crosses = _trend_cleanliness(df)
    if pct_above >= 80 and crosses <= 4:
        score += 16; bits.append(f"clean trend ({pct_above:.0f}% above EMA20, {crosses} whipsaws)")
    elif pct_above >= 60 and crosses <= 8:
        score += 6; bits.append("acceptable trend")
    else:
        score -= 10; bits.append(f"choppy trend ({crosses} EMA20 whipsaws)")

    if _higher_lows(df):
        score += 8; bits.append("rising swing lows (support intact)")
    else:
        score -= 6; bits.append("no clear higher-low support")

    score = max(0.0, min(100.0, round(score, 0)))
    if score >= 80:
        label = "ELITE"
    elif score >= 65:
        label = "GOOD"
    elif score >= 50:
        label = "AVERAGE"
    else:
        label = "POOR"
    return StructureQuality(score, label, bits)


# ═════════════════════════════════════════════════════════════════════════════
# 7) POSITION-MANAGEMENT INTELLIGENCE
# ═════════════════════════════════════════════════════════════════════════════
def build_management_plan(entry: float, stop: float, t1: float, p_t1: Optional[float],
                          support: Optional[float]) -> ManagementPlan:
    why = [
        f"Invalidation ₹{stop:,.0f}: thesis is wrong if price closes below it "
        f"({'just under structure' if support else 'beyond the ATR noise floor'}) — exit, no averaging down.",
        f"Scale out ₹{t1:,.0f} (T1{f', ~{p_t1*100:.0f}% analog reach' if p_t1 else ''}): "
        f"book ½, move the rest to breakeven — converts an open risk into a free runner.",
        "Trail the remainder on EMA20 (daily close): rides trend continuation without "
        "guessing a top; no new indicator, EMA20 is the same support the entry used.",
        "Continuation: hold while daily closes hold EMA20; exit the runner on the first "
        "daily CLOSE below EMA20 (not an intraday wick).",
    ]
    return ManagementPlan(round(stop, 2), round(t1, 2), "EMA20 daily close",
                          "exit on daily close below EMA20", why)


# ═════════════════════════════════════════════════════════════════════════════
# 8) ORCHESTRATOR — FINAL TRADE QUALITY SCORE
# ═════════════════════════════════════════════════════════════════════════════
def assess_trade(df: pd.DataFrame, *, entry: float, stop: float, atr: float, t1: float,
                 t2: float, entry_context: str, extension_pct: float, rs_status: str,
                 vol_ctx: Optional[dict] = None, regime: str = "", breadth: float = 50,
                 participation: float = 50, leadership: float = 50,
                 sector_grade: Optional[str] = None, earnings_days: Optional[int] = None,
                 cohort: Optional[dict] = None, expected: Optional[dict] = None,
                 calib_p1: Optional[float] = None, calib_p2: Optional[float] = None
                 ) -> TradeQuality:
    """Combine all eight assessments into a grade + risk tier + size factor.

    PHILOSOPHY: this is a RISK MANAGER, not a gatekeeper. It does NOT hide trades —
    the trader makes the final call. It SURFACES the risk (grade + explicit flags),
    SIZES it down (lower quality / more risk ⇒ smaller size, floored so it stays
    actionable), and labels a tier (PRIME / CAUTION / HIGH_RISK). Better to see some
    stocks with their risk on a choppy day than to be shown nothing."""
    overhead = _nearest_overhead(df, entry, 160)
    support = _nearest_support_below(df, entry)

    stop_q = assess_stop(df, entry, stop, atr)
    # adopt a safer suggested stop when the original is fragile (then re-evaluate geometry)
    final_stop = stop
    if stop_q.suggested_stop and stop_q.label in ("CROWDED_STOP", "DANGEROUS_STOP"):
        final_stop = stop_q.suggested_stop
        stop_q2 = assess_stop(df, entry, final_stop, atr)
        # keep the better-scoring of the two, but remember we widened the stop
        if stop_q2.score > stop_q.score:
            stop_q2.suggested_stop = final_stop
            stop_q2.size_factor = min(stop_q2.size_factor, 0.75)  # still size down for the fix
            stop_q = stop_q2

    rps = max(entry - final_stop, 1e-9)
    entry_q = assess_entry(df, entry, atr, entry_context=entry_context,
                           extension_pct=extension_pct, vol_ctx=vol_ctx,
                           overhead=overhead, risk_per_share=rps)
    target_q = assess_targets(entry, t1, t2, overhead=overhead, cohort=cohort,
                              expected=expected, calib_p1=calib_p1, calib_p2=calib_p2)
    death = detect_death_zone(df, entry=entry, atr=atr, entry_context=entry_context,
                              extension_pct=extension_pct, vol_ctx=vol_ctx, overhead=overhead,
                              sector_grade=sector_grade, breadth=breadth,
                              participation=participation, earnings_days=earnings_days)
    trap = detect_liquidity_trap(df, entry_context=entry_context, vol_ctx=vol_ctx,
                                 rs_status=rs_status, leadership=leadership)
    structure = assess_structure(df, entry, atr)
    plan = build_management_plan(entry, final_stop, t1, target_q.p_t1, support)

    # ── combine (equal weight on the four quality axes, then risk penalties) ──
    base = (structure.score + entry_q.score + stop_q.score + target_q.realism) / 4.0
    penalty = 0.0
    if death.level == "MEDIUM":
        penalty += 8
    if death.level == "HIGH":
        penalty += 18
    if trap.level == "MEDIUM":
        penalty += 6
    if trap.level == "HIGH":
        penalty += 14
    final = max(0.0, min(100.0, round(base - penalty, 0)))

    if final >= 80:
        grade = "A"
    elif final >= 70:
        grade = "B"
    elif final >= 60:
        grade = "C"
    else:
        grade = "D"                       # lowest grade — shown, never hidden

    # ── caution flags: the explicit risks the trader should weigh (never hide) ──
    flags: list[str] = []
    if structure.label in ("POOR", "AVERAGE"):
        flags.append(f"{structure.label.lower()} structure")
    if entry_q.label == "FOMO_ENTRY":
        flags.append("FOMO/chase entry")
    elif entry_q.label == "LATE_ENTRY":
        flags.append("late entry")
    if stop_q.label == "DANGEROUS_STOP":
        flags.append("fragile stop — use the suggested level")
    elif stop_q.label == "CROWDED_STOP":
        flags.append("crowded stop")
    if death.level != "LOW":
        flags.append(f"{death.level.lower()} death-zone")
    if trap.level != "LOW":
        flags.append(f"{trap.level.lower()} liquidity-trap")
    if target_q.p_t1 is not None and target_q.p_t1 < 0.25:
        flags.append(f"T1 reach only {target_q.p_t1*100:.0f}%")

    # ── continuous sizing: quality grade × risk multipliers, floored so a low-grade
    #    name is still actionable (small), never zero. The trader decides; we de-risk.
    grade_mult = {"A": 1.0, "B": 0.85, "C": 0.6, "D": 0.4}[grade]
    size_factor = round(max(0.25, min(
        1.0, grade_mult * stop_q.size_factor * death.size_factor * trap.size_factor)), 2)

    severe = (entry_q.label == "FOMO_ENTRY" or stop_q.label == "DANGEROUS_STOP"
              or death.level == "HIGH" or grade == "D"
              or (target_q.p_t1 is not None and target_q.p_t1 < 0.20))
    # PRIME = genuinely clean: A/B grade AND GOOD/ELITE structure AND a good entry AND
    # no severe risk. Everything else stays VISIBLE as CAUTION (your-call, smaller size)
    # or HIGH_RISK (loud flags, smallest size) — nothing is hidden.
    clean_struct = structure.label in ("ELITE", "GOOD")
    clean_entry = entry_q.label in ("IDEAL_ENTRY", "GOOD_ENTRY")
    recommended = grade in ("A", "B") and clean_struct and clean_entry and not severe
    risk_tier = "PRIME" if recommended else ("HIGH_RISK" if severe else "CAUTION")

    headline = flags if flags else [f"clean {structure.label.lower()} structure"]
    return TradeQuality(final, grade, risk_tier, recommended, round(final_stop, 2),
                        size_factor, structure, entry_q, stop_q, target_q, death, trap,
                        plan, headline)
