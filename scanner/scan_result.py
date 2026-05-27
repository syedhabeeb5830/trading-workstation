"""
scanner/scan_result.py  —  Observable Scan Pipeline (Phase 6)
=============================================================
Every ticker produces a ScanResult — pass or fail — with full audit trail.

KEY CHANGES from Phase 5:
  - Pipeline uses new 3-dimensional scoring (setup/exec/regime)
  - open_risk_inr passed to position sizing
  - Tier replaces grade in all plan outputs
  - Gap-up rejection tracked
  - Regime no longer multiplies score
"""

import os
import sys
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd


@dataclass
class ScanResult:
    ticker:              str
    stage:               str = "DATA"
    passed:              bool = False

    rejection_reasons:   list = field(default_factory=list)
    caution_notes:       list = field(default_factory=list)

    avg_volume:          float = 0.0
    price:               float = 0.0
    atr_pct:             float = 0.0
    atr_val:             float = 0.0
    trend_score:         int   = 0
    rs:                  float = 0.0
    range_pct:           float = 0.0
    volume_ratio:        float = 0.0
    distance_pct:        float = 0.0
    extension_pct:       float = 0.0

    setup_score:         float = 0.0
    exec_score:          float = 0.0
    regime_score:        float = 0.0
    near_miss_score:     float = 0.0

    metrics:             Optional[dict] = None
    plan:                Optional[dict] = None

    def add_rejection(self, reason: str): self.rejection_reasons.append(reason)
    def add_caution(self, note: str):     self.caution_notes.append(note)

    @property
    def is_hard_reject(self) -> bool:
        return self.stage in ("DATA", "LIQUIDITY", "ATR")

    @property
    def is_near_miss(self) -> bool:
        return not self.passed and not self.is_hard_reject

    @property
    def primary_rejection(self) -> str:
        return self.rejection_reasons[0] if self.rejection_reasons else "Unknown"

    @property
    def composite_score(self) -> float:
        """Blended display score: 60% setup + 25% exec + 15% regime."""
        return round(0.60 * self.setup_score +
                     0.25 * self.exec_score  +
                     0.15 * self.regime_score, 1)


def _compute_near_miss_score(r: ScanResult, config: dict) -> float:
    if r.is_hard_reject:
        return 0.0
    score  = 25 * min(1.0, max(0.0, (r.rs + 5) / 15))
    score += 20 * (r.trend_score / 4.0)
    score += 20 * min(1.0, max(0.0, 1 - r.range_pct / config["max_consolidation_pct"]))
    score += 20 * min(1.0, max(0.0, 1 - r.distance_pct / (config["breakout_proximity_pct"] * 3)))
    score += 15 * min(1.0, max(0.0, 1 - r.volume_ratio))
    return round(score, 1)


def run_observable_scan(config: dict,
                         open_risk_inr: float = 0.0) -> tuple[dict, list]:
    """
    Full observable pipeline. Returns (regime, list[ScanResult]).
    open_risk_inr fed to position sizing for portfolio heat.
    """
    try:
        from scanner.scanner import (
            get_market_regime, fetch_stock_data,
            passes_liquidity_gate, passes_atr_gate,
            compute_trend_quality, compute_relative_strength,
            compute_consolidation, compute_volume_behavior,
            compute_breakout_proximity,
        )
        from scanner.trade_engine import (
            compute_setup_score, compute_exec_score,
            compute_regime_score, build_trade_plan
        )
        from scanner.watchlist import WATCHLIST
    except ModuleNotFoundError:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from scanner.scanner import (
            get_market_regime, fetch_stock_data,
            passes_liquidity_gate, passes_atr_gate,
            compute_trend_quality, compute_relative_strength,
            compute_consolidation, compute_volume_behavior,
            compute_breakout_proximity,
        )
        from scanner.trade_engine import (
            compute_setup_score, compute_exec_score,
            compute_regime_score, build_trade_plan
        )
        from scanner.watchlist import WATCHLIST

    regime  = get_market_regime(config)
    results = []

    regime_str    = regime["regime"]
    regime_str_val = regime["strength"]
    reg_score      = compute_regime_score(regime_str, regime_str_val)

    for ticker in WATCHLIST:
        r = ScanResult(ticker=ticker)

        # ── DATA ──────────────────────────────────────────────────────────────
        df = fetch_stock_data(ticker, config)
        if df is None:
            r.add_rejection("No data or insufficient history")
            results.append(r); continue

        r.price = float(df["Close"].iloc[-1])

        # ── LIQUIDITY ─────────────────────────────────────────────────────────
        r.stage = "LIQUIDITY"
        r.avg_volume = float(df["Volume"].tail(20).mean())

        if r.avg_volume < config["min_avg_volume"]:
            r.add_rejection(
                f"Low volume ({r.avg_volume:,.0f} < {config['min_avg_volume']:,} min)")
            results.append(r); continue
        if r.price < config["min_price"]:
            r.add_rejection(f"Price too low (₹{r.price:.0f} < ₹{config['min_price']})")
            results.append(r); continue

        # ── ATR ───────────────────────────────────────────────────────────────
        r.stage = "ATR"
        from scanner.scanner import compute_atr_series
        atr_series = compute_atr_series(df)
        r.atr_val  = float(atr_series.iloc[-1])
        r.atr_pct  = round((r.atr_val / r.price) * 100, 2) if r.price else 0.0

        if r.atr_pct > config["max_atr_pct"]:
            r.add_rejection(
                f"ATR too high ({r.atr_pct:.1f}% > {config['max_atr_pct']}%)")
            results.append(r); continue
        if r.atr_pct < config["min_atr_pct"]:
            r.add_rejection(
                f"ATR too low ({r.atr_pct:.1f}% < {config['min_atr_pct']}%)")
            results.append(r); continue

        # ── TREND ─────────────────────────────────────────────────────────────
        r.stage = "TREND"
        trend         = compute_trend_quality(df)
        r.trend_score = trend["score_raw"]

        if r.trend_score < config["min_trend_score"]:
            rs_           = compute_relative_strength(df, regime["index_df"])
            consolidation = compute_consolidation(df, config)
            volume        = compute_volume_behavior(df, config)
            breakout      = compute_breakout_proximity(df, config)
            r.rs          = rs_
            r.range_pct   = consolidation["range_pct"]
            r.volume_ratio = volume["volume_ratio"]
            r.distance_pct = breakout["distance_pct"]

            conds     = trend["conditions"]
            failed    = [k for k, v in conds.items() if not v]
            label_map = {
                "above_sma20": "below 20 SMA", "above_sma50": "below 50 SMA",
                "sma20_above_sma50": "20<50 SMA", "above_sma200": "below 200 SMA"
            }
            r.add_rejection(
                f"Weak trend ({r.trend_score}/4) — "
                + ", ".join(label_map.get(f, f) for f in failed)
            )
            r.near_miss_score = _compute_near_miss_score(r, config)
            results.append(r); continue

        # ── SOFT FILTERS (score, not hard reject) ────────────────────────────
        r.stage = "SOFT_FILTERS"
        rs            = compute_relative_strength(df, regime["index_df"])
        consolidation = compute_consolidation(df, config)
        volume        = compute_volume_behavior(df, config)
        breakout      = compute_breakout_proximity(df, config)

        r.rs            = rs
        r.range_pct     = consolidation["range_pct"]
        r.volume_ratio  = volume["volume_ratio"]
        r.distance_pct  = breakout["distance_pct"]

        rejected = False

        # These are now soft thresholds — set wider, penalise in score
        if rs < config["min_rs"]:
            r.add_rejection(
                f"Weak RS ({rs:+.1f}% < {config['min_rs']:+.1f}% floor)")
            rejected = True

        if consolidation["range_pct"] > config["max_consolidation_pct"]:
            r.add_rejection(
                f"Range too wide ({consolidation['range_pct']:.1f}%"
                f" > {config['max_consolidation_pct']}%)")
            rejected = True

        if rejected:
            r.near_miss_score = _compute_near_miss_score(r, config)
            results.append(r); continue

        # ── SCORE ─────────────────────────────────────────────────────────────
        r.stage = "SCORED"
        metrics = {
            "trend":             trend,
            "relative_strength": rs,
            "consolidation":     consolidation,
            "volume":            volume,
            "breakout":          breakout,
            "atr_pct":           r.atr_pct,
        }
        r.metrics = metrics

        # Compute entry to get exec_score inputs
        from scanner.trade_engine import compute_entry, compute_stops
        entry_data = compute_entry(df, config)
        stop_data  = compute_stops(df, r.atr_val, entry_data["entry_price"], config)

        r.setup_score  = compute_setup_score(metrics, config)
        r.exec_score   = compute_exec_score(metrics, entry_data, stop_data, config)
        r.regime_score = reg_score

        # ── PLAN ──────────────────────────────────────────────────────────────
        r.stage = "PLANNED"
        plan = build_trade_plan(
            ticker, df, r.atr_val,
            r.setup_score, r.exec_score, r.regime_score,
            metrics, regime_str, regime_str_val,
            open_risk_inr, config
        )
        r.plan          = plan
        r.extension_pct = plan.get("extension_pct", 0.0)

        if not volume["is_drying_up"]:
            r.add_caution(f"Volume not drying up yet ({volume['volume_ratio']:.2f}× avg)")

        if plan["tier"] == "AVOID":
            rr = plan.get("rr_t1", 0)
            if rr < config["min_rr_ratio"]:
                r.add_rejection(
                    f"RR too low ({rr:.1f}x < {config['min_rr_ratio']:.1f}x)")
            elif regime_str == "BEAR":
                r.add_rejection("BEAR regime — no new longs")
            else:
                r.add_rejection(
                    f"Setup score too low for deployment "
                    f"(setup {r.setup_score:.0f} < PILOT threshold)")
            r.near_miss_score = _compute_near_miss_score(r, config)
            results.append(r); continue

        r.passed = True
        results.append(r)

    return regime, results


def get_near_misses(results: list, n: int = 5) -> list:
    near = [r for r in results if r.is_near_miss]
    near.sort(key=lambda r: r.near_miss_score, reverse=True)
    return near[:n]


def get_breadth_funnel(results: list) -> dict:
    stage_rank = {
        "DATA": 0, "LIQUIDITY": 1, "ATR": 2,
        "TREND": 3, "SOFT_FILTERS": 4, "SCORED": 5, "PLANNED": 6,
    }
    def _reached(r, stage):
        return stage_rank.get(r.stage, 0) >= stage_rank[stage]

    return {
        "total":          len(results),
        "passed_data":    sum(1 for r in results if _reached(r, "LIQUIDITY")),
        "passed_liq":     sum(1 for r in results if _reached(r, "ATR")),
        "passed_atr":     sum(1 for r in results if _reached(r, "TREND")),
        "passed_trend":   sum(1 for r in results if _reached(r, "SOFT_FILTERS")),
        "passed_filters": sum(1 for r in results if _reached(r, "SCORED")),
        "scored":         sum(1 for r in results if r.stage in ("SCORED", "PLANNED")),
        "actionable":     sum(1 for r in results if r.passed),
    }