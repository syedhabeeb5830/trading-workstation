"""
scanner/trade_engine.py  —  Trade Planning Engine  (Phase 6 rebuild)
=====================================================================
CORE CHANGE from old engine:
  OLD: final_score = setup_score × regime_strength  ← DESTROYS SIGNAL
  NEW: setup_score independent; regime sets TIER CAP ← PRESERVES SIGNAL

Three independent scoring dimensions:
  setup_score  (0-100)  — chart quality, regime-agnostic
  regime_score (0-100)  — market environment
  exec_score   (0-100)  — tradability (proximity, vol, RR)

Deployment tier determined by:
  1. setup_score threshold
  2. regime_cap ceiling
  3. RR hard gate

Position sizing respects tier × portfolio heat.

Engines:
  compute_setup_score()      — pure chart quality
  compute_exec_score()       — tradability
  compute_regime_score()     — market quality
  classify_tier()            — deployment decision
  compute_entry/stops/targets — unchanged mechanics
  compute_position_size()    — tier-aware sizing with heat check
  build_trade_plan()         — assembles everything
"""

import pandas as pd
import math


# ═══════════════════════════════════════════════════════════════
# SCORE ENGINES  (three independent dimensions)
# ═══════════════════════════════════════════════════════════════

def compute_setup_score(metrics: dict, config: dict) -> float:
    """
    Pure chart quality. Regime-agnostic.
    A great chart setup remains great regardless of market environment.
    Range 0-100.
    """
    w = config["setup_weights"]

    # Trend (0-1)
    trend_raw = metrics["trend"]["score_norm"]

    # RS (0-1): map -10…+15 → 0…1
    rs = metrics["relative_strength"]
    rs_raw = min(1.0, max(0.0, (rs + 10) / 25))

    # Compression (0-1): tighter range = better
    rng = metrics["consolidation"]["range_pct"]
    comp_raw = min(1.0, max(0.0, 1 - rng / 6.0))
    inside   = metrics["consolidation"]["inside_bar_count"]
    comp_raw = min(1.0, comp_raw + min(0.15, inside * 0.05))

    # Volume (0-1): drying up = better
    vol     = metrics["volume"]["volume_ratio"]
    vol_raw = min(1.0, max(0.0, 1 - vol))
    if metrics["volume"]["is_drying_up"]:
        vol_raw = max(vol_raw, 0.55)

    raw = (
        w["trend_quality"]       * trend_raw +
        w["relative_strength"]   * rs_raw    +
        w["compression_quality"] * comp_raw  +
        w["volume_behavior"]     * vol_raw
    )
    return round(raw * 100, 1)


def compute_exec_score(metrics: dict, entry_data: dict,
                       stop_data: dict, config: dict) -> float:
    """
    Tradability score. How clean is the execution setup?
    Breakout proximity + volatility quality.
    Range 0-100.
    """
    w = config["exec_weights"]

    # Proximity (0-1): closer to breakout = better
    dist    = metrics["breakout"]["distance_pct"]
    prox    = config["breakout_proximity_pct"]
    prx_raw = min(1.0, max(0.0, 1 - dist / (prox * 1.5)))

    # Volatility quality (sweet spot 1.5-4%)
    atr_pct = metrics["atr_pct"]
    if 1.5 <= atr_pct <= 4.0:
        vol_q = 1.0
    elif atr_pct < 1.5:
        vol_q = atr_pct / 1.5
    else:
        vol_q = max(0.0, 1 - (atr_pct - 4.0) / 3.0)

    raw = w["breakout_proximity"] * prx_raw + w["volatility_quality"] * vol_q
    return round(raw * 100, 1)


def compute_regime_score(regime: str, strength: float) -> float:
    """
    Market environment quality. 0-100.
    BULL strong = 85-100, BULL weak = 65-84
    NEUTRAL = 35-64, BEAR = 0-34
    """
    if regime == "BULL":
        return round(60 + 40 * strength, 1)
    elif regime == "NEUTRAL":
        return round(30 + 35 * strength, 1)
    else:  # BEAR
        return round(15 * strength, 1)


def classify_tier(setup_score: float, exec_score: float,
                  regime: str, rr: float, config: dict) -> str:
    """
    Deployment tier — replaces binary ACTIONABLE/AVOID.

    Logic:
      1. Regime sets a CEILING (cap) on what tier is allowed
      2. setup_score determines tier within that ceiling
      3. RR hard gate: below min_rr → AVOID regardless

    This means a great setup in NEUTRAL market = TIER_2 (75% size)
    NOT destroyed to AVOID as in old model.
    """
    # RR gate is non-negotiable
    if rr < config["min_rr_ratio"]:
        return "AVOID"

    # Regime ceiling
    regime_cap = config["regime_tier_cap"].get(regime, "TIER_2")
    if regime_cap == "AVOID":
        return "AVOID"

    thresholds = config["tier_thresholds"]

    # Determine natural tier from setup score
    if setup_score >= thresholds["TIER_1"]:
        natural_tier = "TIER_1"
    elif setup_score >= thresholds["TIER_2"]:
        natural_tier = "TIER_2"
    elif setup_score >= thresholds["PILOT"]:
        natural_tier = "PILOT"
    else:
        return "AVOID"

    # Apply regime ceiling
    tier_order = ["TIER_1", "TIER_2", "PILOT", "WATCH", "AVOID"]
    cap_idx    = tier_order.index(regime_cap)
    nat_idx    = tier_order.index(natural_tier)
    final_idx  = max(cap_idx, nat_idx)   # higher index = more conservative

    return tier_order[final_idx]


# ═══════════════════════════════════════════════════════════════
# ENTRY ENGINE
# ═══════════════════════════════════════════════════════════════

def compute_entry(df: pd.DataFrame, config: dict) -> dict:
    breakout_level = float(df["High"].tail(20).max())
    current_close  = float(df["Close"].iloc[-1])
    buffer_pct     = config["entry_buffer_pct"] / 100
    entry_price    = round(breakout_level * (1 + buffer_pct), 2)
    extension_pct  = ((current_close - breakout_level) / breakout_level) * 100

    # Gap-up check: compare today's open vs yesterday's high
    gap_up_pct = 0.0
    if len(df) >= 2:
        today_open    = float(df["Open"].iloc[-1])
        yesterday_high = float(df["High"].iloc[-2])
        if yesterday_high > 0:
            gap_up_pct = ((today_open - yesterday_high) / yesterday_high) * 100

    is_gapped_up = gap_up_pct > config.get("max_gap_up_pct", 2.5)

    return {
        "breakout_level":  round(breakout_level, 2),
        "entry_price":     entry_price,
        "current_close":   round(current_close, 2),
        "extension_pct":   round(extension_pct, 2),
        "gap_up_pct":      round(gap_up_pct, 2),
        "is_gapped_up":    is_gapped_up,
    }


# ═══════════════════════════════════════════════════════════════
# STOP ENGINE
# ═══════════════════════════════════════════════════════════════

def compute_stops(df: pd.DataFrame, atr_val: float,
                  entry_price: float, config: dict) -> dict:
    lookback  = config["stop_swing_lookback"]
    atr_mult  = config["stop_atr_multiple"]
    method    = config["stop_selection"]

    atr_stop       = round(entry_price - (atr_val * atr_mult), 2)
    swing_low_stop = round(float(df["Low"].tail(lookback).min()), 2)

    if method == "atr":
        final_stop, stop_type = atr_stop, "ATR"
    elif method == "swing":
        final_stop, stop_type = swing_low_stop, "SWING"
    else:
        if atr_stop > swing_low_stop:
            final_stop, stop_type = atr_stop, "ATR (tighter)"
        else:
            final_stop, stop_type = swing_low_stop, "SWING (tighter)"

    if final_stop >= entry_price:
        final_stop, stop_type = atr_stop, "ATR (fallback)"

    risk_per_share = round(entry_price - final_stop, 2)
    risk_pct       = round((risk_per_share / entry_price) * 100, 2)

    return {
        "atr_stop":        atr_stop,
        "swing_low_stop":  swing_low_stop,
        "final_stop":      final_stop,
        "stop_type":       stop_type,
        "risk_per_share":  risk_per_share,
        "risk_pct":        risk_pct,
    }


# ═══════════════════════════════════════════════════════════════
# TARGET ENGINE
# ═══════════════════════════════════════════════════════════════

def _compute_atr_series(df: pd.DataFrame, window: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(window=window).mean()


def compute_targets(entry_price: float, risk_per_share: float,
                    df: pd.DataFrame, config: dict) -> dict:
    r_levels = config["target_r_levels"]
    t1 = round(entry_price + risk_per_share * r_levels[0], 2)
    t2 = round(entry_price + risk_per_share * r_levels[1], 2)

    n          = config["consolidation_days"]
    recent     = df.tail(n)
    consol_rng = float(recent["High"].max()) - float(recent["Low"].min())
    brk_high   = float(df["High"].tail(20).max())
    measured_move = round(brk_high + consol_rng * config["breakout_projection_multiple"], 2)

    atr_val       = float(_compute_atr_series(df).iloc[-1])
    trailing_stop = round(float(df["Close"].iloc[-1]) - atr_val, 2)

    return {
        "t1":              t1,
        "t2":              t2,
        "measured_move":   measured_move,
        "trailing_stop":   trailing_stop,
        "r1_distance_pct": round(((t1 - entry_price) / entry_price) * 100, 2),
        "r2_distance_pct": round(((t2 - entry_price) / entry_price) * 100, 2),
    }


# ═══════════════════════════════════════════════════════════════
# POSITION SIZING  (tier-aware + portfolio heat)
# ═══════════════════════════════════════════════════════════════

def compute_position_size(entry_price: float, stop_price: float,
                           tier: str, open_risk_inr: float,
                           config: dict) -> dict:
    """
    Tier-aware fixed-fractional sizing with portfolio heat check.

    open_risk_inr = sum of max_loss across all currently open trades
    This prevents silently exceeding max_portfolio_heat.

    Uses live Kite capital when available, else config["account_capital"].
    """
    # Live capital with config fallback — sizing auto-adjusts after drawdowns
    try:
        from scanner.capital import get_effective_capital
        capital = get_effective_capital(config).capital
    except Exception:
        capital = config["account_capital"]
    tier_mult = config["tier_size_pct"].get(tier, 0.0)
    max_heat  = config["max_portfolio_heat"]

    # Base risk for this trade
    base_risk_inr = capital * config["max_risk_per_trade"]
    trade_risk    = base_risk_inr * tier_mult

    # Portfolio heat guard
    remaining_heat = (capital * max_heat) - open_risk_inr
    trade_risk     = min(trade_risk, max(0.0, remaining_heat))

    risk_per_share = entry_price - stop_price

    if risk_per_share <= 0 or trade_risk <= 0:
        return {
            "quantity": 0, "capital_deployed": 0, "capital_pct": 0,
            "max_loss_inr": 0, "max_risk_inr": round(trade_risk, 2),
            "tier_size_pct": round(tier_mult * 100),
            "heat_remaining_pct": round(remaining_heat / capital * 100, 2),
            "error": "No risk budget or invalid stop",
        }

    quantity         = math.floor(trade_risk / risk_per_share)
    capital_deployed = round(quantity * entry_price, 2)
    max_loss_inr     = round(quantity * risk_per_share, 2)
    capital_pct      = round(capital_deployed / capital * 100, 2)

    return {
        "quantity":            quantity,
        "capital_deployed":    capital_deployed,
        "capital_pct":         capital_pct,
        "max_loss_inr":        max_loss_inr,
        "max_risk_inr":        round(trade_risk, 2),
        "risk_per_share":      round(risk_per_share, 2),
        "tier_size_pct":       round(tier_mult * 100),
        "heat_remaining_pct":  round(remaining_heat / capital * 100, 2),
    }


# ═══════════════════════════════════════════════════════════════
# EXEC STATUS
# ═══════════════════════════════════════════════════════════════

def classify_exec_status(entry_price: float, current_close: float,
                          extension_pct: float, tier: str,
                          is_gapped_up: bool, config: dict) -> str:
    if tier == "AVOID":
        return "AVOID"

    if is_gapped_up:
        return "GAPPED"    # new: gap-up on entry day

    if extension_pct > config["max_extension_pct"]:
        return "EXTENDED"

    distance_pct = ((entry_price - current_close) / entry_price) * 100
    if distance_pct <= config["ready_max_distance_pct"]:
        return "READY"

    return "WATCH"


# ═══════════════════════════════════════════════════════════════
# ASSEMBLER
# ═══════════════════════════════════════════════════════════════

def build_trade_plan(ticker: str, df: pd.DataFrame, atr_val: float,
                     setup_score: float, exec_score: float,
                     regime_score: float, metrics: dict,
                     regime: str, regime_strength: float,
                     open_risk_inr: float,
                     config: dict) -> dict:
    """
    Builds complete trade plan. open_risk_inr is passed in from
    portfolio engine so sizing respects heat.
    """
    entry  = compute_entry(df, config)
    stops  = compute_stops(df, atr_val, entry["entry_price"], config)
    targets = compute_targets(
        entry["entry_price"], stops["risk_per_share"], df, config
    )

    rr_t1 = round(
        (targets["t1"] - entry["entry_price"]) / stops["risk_per_share"], 2
    ) if stops["risk_per_share"] > 0 else 0.0
    rr_t2 = round(
        (targets["t2"] - entry["entry_price"]) / stops["risk_per_share"], 2
    ) if stops["risk_per_share"] > 0 else 0.0

    tier   = classify_tier(setup_score, exec_score, regime, rr_t1, config)
    status = classify_exec_status(
        entry["entry_price"], entry["current_close"],
        entry["extension_pct"], tier, entry["is_gapped_up"], config
    )

    sizing = compute_position_size(
        entry["entry_price"], stops["final_stop"],
        tier, open_risk_inr, config
    )

    sector = config.get("sector_map", {}).get(ticker, "OTHER")

    return {
        "ticker":            ticker,
        "sector":            sector,
        "setup_score":       setup_score,
        "exec_score":        exec_score,
        "regime_score":      regime_score,
        "tier":              tier,
        "status":            status,

        # Entry
        "breakout_level":    entry["breakout_level"],
        "entry_price":       entry["entry_price"],
        "current_close":     entry["current_close"],
        "extension_pct":     entry["extension_pct"],
        "gap_up_pct":        entry["gap_up_pct"],
        "is_gapped_up":      entry["is_gapped_up"],

        # Stops
        "stop_price":        stops["final_stop"],
        "stop_type":         stops["stop_type"],
        "risk_per_share":    stops["risk_per_share"],
        "risk_pct":          stops["risk_pct"],

        # Targets
        "t1":                targets["t1"],
        "t2":                targets["t2"],
        "measured_move":     targets["measured_move"],
        "trailing_stop":     targets["trailing_stop"],
        "r1_distance_pct":   targets["r1_distance_pct"],
        "r2_distance_pct":   targets["r2_distance_pct"],

        # RR
        "rr_t1":             rr_t1,
        "rr_t2":             rr_t2,

        # Sizing
        "quantity":          sizing["quantity"],
        "capital_deployed":  sizing["capital_deployed"],
        "capital_pct":       sizing["capital_pct"],
        "max_loss_inr":      sizing["max_loss_inr"],
        "tier_size_pct":     sizing.get("tier_size_pct", 0),
        "heat_remaining_pct": sizing.get("heat_remaining_pct", 0),

        # Metrics (for display + analytics)
        "atr_pct":           metrics["atr_pct"],
        "relative_strength": metrics["relative_strength"],
        "range_pct":         metrics["consolidation"]["range_pct"],
        "volume_ratio":      metrics["volume"]["volume_ratio"],
        "trend_score":       metrics["trend"]["score_raw"],
        "inside_bar_count":  metrics["consolidation"]["inside_bar_count"],
    }