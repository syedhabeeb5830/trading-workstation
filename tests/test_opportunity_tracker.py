"""
tests/test_opportunity_tracker.py — Opportunity Cost Analytics Tests
====================================================================
Tests are fully offline — no yfinance calls, no filesystem writes beyond
a tmp directory. All data is synthetic.

Coverage:
  1. Status mapping (_map_oc_status)
  2. Candidate deduplication (_build_candidate_index)
  3. Bar-by-bar resolution logic (_resolve_candidate)
     - T1 hit
     - T2 hit
     - Stop hit
     - No exit in window → OPEN
     - Same-bar stop+target conflict → conservative stop wins
  4. compute_opportunity_stats calculations
     - PF, expectancy, win_rate verified by hand
     - Bucket isolation (READY/WATCH/REJECTED/ALL)
     - None returned for buckets below MIN_SAMPLE
  5. load_opportunity_log handles missing / empty files
  6. resolve_opportunity_outcomes integration (mocked yfinance)
  7. Sample historical-style data end-to-end: verifies that
     filter-hurting scenario (REJECTED PF > READY PF) is detectable
"""

import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import numpy as np

# Make sure package root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics.opportunity_tracker import (
    _map_oc_status,
    _build_candidate_index,
    _resolve_candidate,
    compute_opportunity_stats,
    load_opportunity_log,
    get_opportunity_log_path,
    OPP_COLUMNS,
    MIN_SAMPLE,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _make_ohlcv_df(bars: list) -> pd.DataFrame:
    """
    bars = [(open, high, low, close, volume), ...]
    Returns a DataFrame indexed by consecutive business dates.
    """
    index = pd.bdate_range("2026-01-02", periods=len(bars), freq="B")
    df = pd.DataFrame(bars, columns=["Open", "High", "Low", "Close", "Volume"],
                      index=index)
    return df


def _make_candidate_row(
    ticker="TEST.NS",
    scan_date="2026-01-02",
    oc_status="READY",
    raw_status="READY",
    entry=100.0,
    stop=95.0,
    t1=110.0,
    t2=115.0,
    rr_t1=2.0,
    score=70,
) -> pd.Series:
    return pd.Series({
        "ticker":       ticker,
        "scan_date":    scan_date,
        "score":        score,
        "raw_status":   raw_status,
        "oc_status":    oc_status,
        "entry_price":  entry,
        "stop_price":   stop,
        "t1":           t1,
        "t2":           t2,
        "rr_t1":        rr_t1,
        "mfe_pct":      "",
        "mae_pct":      "",
        "r_multiple":   "",
        "final_outcome": "OPEN",
        "resolved":     "False",
    })


def _make_opp_log(rows: list) -> pd.DataFrame:
    """
    rows = [dict(ticker, scan_date, oc_status, r_multiple, resolved), ...]
    Missing keys default to sensible values.
    setup_id is auto-synthesized as SWING_{ticker}_{scan_date} (replacing dots)
    to match what _build_candidate_index generates.
    """
    defaults = {
        "strategy":      "SWING",
        "last_seen":     "",
        "days_active":   1,
        "score":         60,
        "raw_status":    "READY",
        "entry_price":   100.0,
        "stop_price":     95.0,
        "t1":            110.0,
        "t2":            115.0,
        "rr_t1":           2.0,
        "mfe_pct":         5.0,
        "mae_pct":        -2.0,
        "final_outcome": "T1_HIT",
        "resolved":      "True",
    }
    filled = []
    for r in rows:
        row = {**defaults, **r}
        # Synthesize setup_id from ticker + scan_date if not provided
        if not row.get("setup_id"):
            tk  = str(row.get("ticker", "")).replace(".", "_")
            sd  = str(row.get("scan_date") or row.get("first_seen", "2026-01-02"))
            row["setup_id"]   = f"SWING_{tk}_{sd}"
        if not row.get("first_seen"):
            row["first_seen"] = row.get("scan_date", "2026-01-02")
        for col in OPP_COLUMNS:
            row.setdefault(col, "")
        filled.append({k: row[k] for k in OPP_COLUMNS})

    df = pd.DataFrame(filled, columns=OPP_COLUMNS)
    for col in ["score", "entry_price", "stop_price", "t1", "t2",
                "rr_t1", "mfe_pct", "mae_pct", "r_multiple", "days_active"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


# ═══════════════════════════════════════════════════
# 1. Status Mapping
# ═══════════════════════════════════════════════════
class TestMapOcStatus(unittest.TestCase):

    def test_ready_maps_to_ready(self):
        self.assertEqual(_map_oc_status("READY"), "READY")

    def test_watch_maps_to_watch(self):
        self.assertEqual(_map_oc_status("WATCH"), "WATCH")

    def test_avoid_maps_to_rejected(self):
        self.assertEqual(_map_oc_status("AVOID"), "REJECTED")

    def test_extended_maps_to_watch(self):
        # EXTENDED = price ran past entry temporarily; setup is still active (WATCH)
        self.assertEqual(_map_oc_status("EXTENDED"), "WATCH")

    def test_gapped_maps_to_rejected(self):
        self.assertEqual(_map_oc_status("GAPPED"), "REJECTED")

    def test_blocked_maps_to_rejected(self):
        self.assertEqual(_map_oc_status("BLOCKED"), "REJECTED")

    def test_case_insensitive(self):
        self.assertEqual(_map_oc_status("ready"), "READY")
        self.assertEqual(_map_oc_status("Watch"), "WATCH")
        self.assertEqual(_map_oc_status("avoid"), "REJECTED")

    def test_unknown_maps_to_rejected(self):
        self.assertEqual(_map_oc_status("FOOBAR"), "REJECTED")


# ═══════════════════════════════════════════════════
# 2. Candidate Deduplication
# ═══════════════════════════════════════════════════
class TestBuildCandidateIndex(unittest.TestCase):

    def _make_scan_df(self, rows):
        cols = [
            "scan_timestamp", "scan_date", "ticker", "status",
            "score", "entry_price", "stop_price", "t1", "t2", "rr_t1",
        ]
        df = pd.DataFrame(rows, columns=cols)
        return df

    def test_deduplicates_multiple_scans_same_day(self):
        rows = [
            ("2026-01-02 09:30:00", "2026-01-02", "AAA.NS", "READY",  70, 100, 95, 110, 115, 2.0),
            ("2026-01-02 14:00:00", "2026-01-02", "AAA.NS", "WATCH",  65, 100, 95, 110, 115, 2.0),
        ]
        df = self._make_scan_df(rows)
        result = _build_candidate_index(df)
        self.assertEqual(len(result), 1)
        # First scan of day — READY, not WATCH
        self.assertEqual(result.iloc[0]["raw_status"], "READY")

    def test_different_tickers_same_day_kept(self):
        rows = [
            ("2026-01-02 09:30:00", "2026-01-02", "AAA.NS", "READY", 70, 100, 95, 110, 115, 2.0),
            ("2026-01-02 09:30:00", "2026-01-02", "BBB.NS", "WATCH", 65, 200, 190, 220, 230, 2.0),
        ]
        df = self._make_scan_df(rows)
        result = _build_candidate_index(df)
        self.assertEqual(len(result), 2)

    def test_same_ticker_different_dates_produces_one_setup(self):
        # New dedup model: same (strategy, ticker) across multiple dates = 1 unique setup.
        # The setup is updated on subsequent scans, not duplicated.
        rows = [
            ("2026-01-02 09:30:00", "2026-01-02", "AAA.NS", "READY", 70, 100, 95, 110, 115, 2.0),
            ("2026-01-05 09:30:00", "2026-01-05", "AAA.NS", "WATCH", 65, 102, 97, 112, 117, 2.0),
        ]
        df = self._make_scan_df(rows)
        result = _build_candidate_index(df)
        self.assertEqual(len(result), 1, "Same setup on consecutive days must be deduplicated")
        row = result.iloc[0]
        self.assertEqual(row["ticker"],     "AAA.NS")
        self.assertEqual(row["first_seen"], "2026-01-02",
                         "first_seen must be the earliest scan date")

    def test_rows_without_entry_price_excluded(self):
        rows = [
            ("2026-01-02 09:30:00", "2026-01-02", "AAA.NS", "READY", 70, "",  95, 110, 115, 2.0),
            ("2026-01-02 09:30:00", "2026-01-02", "BBB.NS", "WATCH", 65, 200, 190, 220, 230, 2.0),
        ]
        df = self._make_scan_df(rows)
        result = _build_candidate_index(df)
        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["ticker"], "BBB.NS")

    def test_oc_status_assigned_correctly(self):
        rows = [
            ("2026-01-02 09:00:00", "2026-01-02", "R.NS",  "READY",    70, 100, 95, 110, 115, 2.0),
            ("2026-01-02 09:00:00", "2026-01-02", "W.NS",  "WATCH",    60, 200, 190, 220, 230, 2.0),
            ("2026-01-02 09:00:00", "2026-01-02", "AV.NS", "AVOID",    30, 300, 285, 330, 345, 2.0),
            ("2026-01-02 09:00:00", "2026-01-02", "EX.NS", "EXTENDED", 40, 400, 380, 440, 460, 2.0),
        ]
        df = self._make_scan_df(rows)
        result = _build_candidate_index(df)
        statuses = dict(zip(result["ticker"], result["oc_status"]))
        self.assertEqual(statuses["R.NS"],  "READY")
        self.assertEqual(statuses["W.NS"],  "WATCH")
        self.assertEqual(statuses["AV.NS"], "REJECTED")
        # EXTENDED maps to WATCH — same setup, price just ran temporarily past entry
        self.assertEqual(statuses["EX.NS"], "WATCH")

    def test_empty_scan_df_returns_empty(self):
        result = _build_candidate_index(pd.DataFrame())
        self.assertTrue(result.empty)


# ═══════════════════════════════════════════════════
# 3. Bar-by-Bar Resolution
# ═══════════════════════════════════════════════════
class TestResolveCandidate(unittest.TestCase):
    """
    All tests mock _fetch_prices so no internet is required.
    Entry=100, Stop=95 (risk=5), T1=110 (2R), T2=115 (3R).
    """

    def _row(self, **kwargs):
        return _make_candidate_row(**kwargs)

    def test_t1_hit_returns_correct_r(self):
        bars = _make_ohlcv_df([
            (100, 105, 99, 104, 1000),    # day 1 — no hit
            (104, 112, 102, 110, 1000),   # day 2 — high ≥ 110 → T1
        ])
        with patch("analytics.opportunity_tracker._fetch_prices", return_value=bars):
            result = _resolve_candidate(self._row())
        self.assertEqual(result["final_outcome"], "T1_HIT")
        self.assertAlmostEqual(result["r_multiple"], 2.0)
        self.assertEqual(result["resolved"], "True")

    def test_t2_hit_returns_1_5x_rr(self):
        # rr_t1=2 → T2 should be rr_t1*1.5 = 3.0R
        bars = _make_ohlcv_df([
            (100, 105, 99, 104, 1000),
            (104, 116, 102, 115, 1000),   # high ≥ 115 → T2
        ])
        with patch("analytics.opportunity_tracker._fetch_prices", return_value=bars):
            result = _resolve_candidate(self._row())
        self.assertEqual(result["final_outcome"], "T2_HIT")
        self.assertAlmostEqual(result["r_multiple"], 3.0)
        self.assertEqual(result["resolved"], "True")

    def test_stop_hit_returns_minus_1r(self):
        bars = _make_ohlcv_df([
            (100, 103, 96, 102, 1000),
            (99,  101, 94, 95,  1000),   # low ≤ 95 → stop
        ])
        with patch("analytics.opportunity_tracker._fetch_prices", return_value=bars):
            result = _resolve_candidate(self._row())
        self.assertEqual(result["final_outcome"], "STOP_HIT")
        self.assertAlmostEqual(result["r_multiple"], -1.0)
        self.assertEqual(result["resolved"], "True")

    def test_same_bar_stop_and_t1_stop_wins(self):
        # Bar touches both T1 (high≥110) and stop (low≤95) — conservative: stop wins
        bars = _make_ohlcv_df([
            (100, 112, 94, 100, 1000),   # day 1 itself — skipped (bar 0)
            (100, 112, 94, 100, 1000),   # day 2 — same bar conflict
        ])
        with patch("analytics.opportunity_tracker._fetch_prices", return_value=bars):
            result = _resolve_candidate(self._row())
        self.assertEqual(result["final_outcome"], "STOP_HIT")
        self.assertAlmostEqual(result["r_multiple"], -1.0)

    def test_no_exit_in_window_returns_open(self):
        # Prices stay between stop and T1 for all bars
        bars = _make_ohlcv_df([
            (100, 105, 96, 102, 1000),
            (101, 108, 97, 104, 1000),
            (103, 109, 98, 106, 1000),
        ])
        with patch("analytics.opportunity_tracker._fetch_prices", return_value=bars):
            result = _resolve_candidate(self._row())
        self.assertEqual(result["final_outcome"], "OPEN")
        self.assertEqual(result["resolved"], "False")
        self.assertIn("mfe_pct", result)
        self.assertIn("mae_pct", result)

    def test_mfe_mae_computed_correctly(self):
        # entry=100, highest_high=115, lowest_low=92
        # MFE = (115-100)/100 * 100 = 15.0%
        # MAE = (92-100)/100 * 100 = -8.0%
        bars = _make_ohlcv_df([
            (100, 115, 92, 100, 1000),   # bar 0 — excluded (scan day)
            (100, 115, 92, 100, 1000),   # bar 1 — T2 hit (115 ≥ 115)
        ])
        with patch("analytics.opportunity_tracker._fetch_prices", return_value=bars):
            result = _resolve_candidate(self._row())
        # MFE and MAE are measured over bar 1 only (bar 0 excluded)
        self.assertAlmostEqual(result["mfe_pct"], 15.0)
        self.assertAlmostEqual(result["mae_pct"], -8.0)

    def test_no_price_data_returns_empty_dict(self):
        with patch("analytics.opportunity_tracker._fetch_prices", return_value=None):
            result = _resolve_candidate(self._row())
        self.assertEqual(result, {})

    def test_invalid_entry_price_returns_empty(self):
        row = _make_candidate_row(entry=0.0)
        with patch("analytics.opportunity_tracker._fetch_prices", return_value=None):
            result = _resolve_candidate(row)
        self.assertEqual(result, {})

    def test_single_bar_window_resolves(self):
        # Only one bar after scan day — can still resolve
        bars = _make_ohlcv_df([
            (100, 105, 96, 102, 1000),  # bar 0 — excluded
            (100, 112, 96, 110, 1000),  # bar 1 — T1 hit
        ])
        with patch("analytics.opportunity_tracker._fetch_prices", return_value=bars):
            result = _resolve_candidate(self._row())
        self.assertEqual(result["final_outcome"], "T1_HIT")

    def test_first_bar_is_scan_day_excluded(self):
        # Bar 0 would be a T1 hit — but it's the scan day and should be skipped
        bars = _make_ohlcv_df([
            (100, 115, 92, 110, 1000),  # scan day — excluded
            (100, 103, 96, 102, 1000),  # day after — no hit
        ])
        with patch("analytics.opportunity_tracker._fetch_prices", return_value=bars):
            result = _resolve_candidate(self._row())
        # Should be OPEN, not T2_HIT
        self.assertEqual(result["final_outcome"], "OPEN")


# ═══════════════════════════════════════════════════
# 4. compute_opportunity_stats — Calculation Validation
# ═══════════════════════════════════════════════════
class TestComputeOpportunityStats(unittest.TestCase):
    """
    Verifies PF, expectancy, win_rate by hand for each bucket.

    Scenario:
      READY    (8 trades): 5 wins @+2R, 3 losses @-1R
        gross_profit=10, gross_loss=3, PF=3.33, exp=0.875, wr=62.5%
      WATCH    (6 trades): 3 wins @+2R, 3 losses @-1R
        gross_profit=6, gross_loss=3, PF=2.00, exp=0.500, wr=50.0%
      REJECTED (7 trades): 5 wins @+2R, 2 losses @-1R
        gross_profit=10, gross_loss=2, PF=5.00, exp=1.143, wr=71.4%
    """

    def _build_df(self):
        rows = (
            # READY — 5 wins, 3 losses
            [{"ticker": f"R{i}", "scan_date": f"2026-01-{i+2:02d}",
              "oc_status": "READY", "r_multiple": 2.0,
              "mfe_pct": 5.0, "mae_pct": -2.0,
              "resolved": "True", "final_outcome": "T1_HIT"}
             for i in range(5)]
            +
            [{"ticker": f"RL{i}", "scan_date": f"2026-01-{i+10:02d}",
              "oc_status": "READY", "r_multiple": -1.0,
              "mfe_pct": 2.0, "mae_pct": -5.0,
              "resolved": "True", "final_outcome": "STOP_HIT"}
             for i in range(3)]
            +
            # WATCH — 3 wins, 3 losses
            [{"ticker": f"W{i}", "scan_date": f"2026-01-{i+2:02d}",
              "oc_status": "WATCH", "r_multiple": 2.0,
              "mfe_pct": 4.0, "mae_pct": -1.5,
              "resolved": "True", "final_outcome": "T1_HIT"}
             for i in range(3)]
            +
            [{"ticker": f"WL{i}", "scan_date": f"2026-01-{i+10:02d}",
              "oc_status": "WATCH", "r_multiple": -1.0,
              "mfe_pct": 1.0, "mae_pct": -4.0,
              "resolved": "True", "final_outcome": "STOP_HIT"}
             for i in range(3)]
            +
            # REJECTED — 5 wins, 2 losses
            [{"ticker": f"J{i}", "scan_date": f"2026-01-{i+2:02d}",
              "oc_status": "REJECTED", "r_multiple": 2.0,
              "mfe_pct": 6.0, "mae_pct": -1.0,
              "resolved": "True", "final_outcome": "T1_HIT"}
             for i in range(5)]
            +
            [{"ticker": f"JL{i}", "scan_date": f"2026-01-{i+10:02d}",
              "oc_status": "REJECTED", "r_multiple": -1.0,
              "mfe_pct": 2.0, "mae_pct": -6.0,
              "resolved": "True", "final_outcome": "STOP_HIT"}
             for i in range(2)]
        )
        return _make_opp_log(rows)

    def setUp(self):
        self.df    = self._build_df()
        self.stats = compute_opportunity_stats(self.df)

    # ── READY ──────────────────────────────────────────────────────────────
    def test_ready_sample_size(self):
        self.assertEqual(self.stats["READY"]["n"], 8)

    def test_ready_win_rate(self):
        self.assertAlmostEqual(self.stats["READY"]["win_rate_pct"], 62.5, places=1)

    def test_ready_profit_factor(self):
        # gross_profit=10, gross_loss=3  → PF=3.33
        self.assertAlmostEqual(self.stats["READY"]["profit_factor"],
                               round(10 / 3, 2), places=2)

    def test_ready_expectancy(self):
        # (5*2 + 3*(-1)) / 8 = 7/8 = 0.875
        self.assertAlmostEqual(self.stats["READY"]["expectancy_r"], 0.875, places=3)

    def test_ready_total_r(self):
        self.assertAlmostEqual(self.stats["READY"]["total_r"], 7.0, places=1)

    # ── WATCH ──────────────────────────────────────────────────────────────
    def test_watch_sample_size(self):
        self.assertEqual(self.stats["WATCH"]["n"], 6)

    def test_watch_win_rate(self):
        self.assertAlmostEqual(self.stats["WATCH"]["win_rate_pct"], 50.0, places=1)

    def test_watch_profit_factor(self):
        # gross_profit=6, gross_loss=3 → PF=2.00
        self.assertAlmostEqual(self.stats["WATCH"]["profit_factor"], 2.00, places=2)

    def test_watch_expectancy(self):
        # (3*2 + 3*(-1)) / 6 = 3/6 = 0.500
        self.assertAlmostEqual(self.stats["WATCH"]["expectancy_r"], 0.5, places=3)

    # ── REJECTED ───────────────────────────────────────────────────────────
    def test_rejected_sample_size(self):
        self.assertEqual(self.stats["REJECTED"]["n"], 7)

    def test_rejected_win_rate(self):
        self.assertAlmostEqual(
            self.stats["REJECTED"]["win_rate_pct"],
            round(5 / 7 * 100, 1), places=1
        )

    def test_rejected_profit_factor(self):
        # gross_profit=10, gross_loss=2 → PF=5.00
        self.assertAlmostEqual(self.stats["REJECTED"]["profit_factor"], 5.00, places=2)

    def test_rejected_expectancy(self):
        # (5*2 + 2*(-1)) / 7 = 8/7 ≈ 1.143
        self.assertAlmostEqual(
            self.stats["REJECTED"]["expectancy_r"],
            round(8 / 7, 3), places=2
        )

    # ── ALL bucket ─────────────────────────────────────────────────────────
    def test_all_bucket_n(self):
        self.assertEqual(self.stats["ALL"]["n"], 21)

    def test_all_bucket_total_r(self):
        # READY 7R + WATCH 3R + REJECTED 8R = 18R
        self.assertAlmostEqual(self.stats["ALL"]["total_r"], 18.0, places=1)

    # ── Filter-hurting detection ────────────────────────────────────────────
    def test_rejected_outperforms_ready(self):
        """In our synthetic data, REJECTED PF > READY PF."""
        rejected_pf = self.stats["REJECTED"]["profit_factor"]
        ready_pf    = self.stats["READY"]["profit_factor"]
        self.assertGreater(rejected_pf, ready_pf)

    # ── None for insufficient sample ───────────────────────────────────────
    def test_none_returned_for_small_sample(self):
        tiny_df = _make_opp_log([
            {"ticker": "X1", "scan_date": "2026-01-02", "oc_status": "READY",
             "r_multiple": 2.0, "resolved": "True"},
            {"ticker": "X2", "scan_date": "2026-01-03", "oc_status": "READY",
             "r_multiple": -1.0, "resolved": "True"},
        ])
        stats = compute_opportunity_stats(tiny_df)
        self.assertIsNone(stats["READY"])

    def test_none_returned_for_empty_df(self):
        stats = compute_opportunity_stats(pd.DataFrame(columns=OPP_COLUMNS))
        self.assertIsNone(stats["READY"])
        self.assertIsNone(stats["WATCH"])
        self.assertIsNone(stats["REJECTED"])
        self.assertIsNone(stats["ALL"])

    # ── Unresolved rows excluded ────────────────────────────────────────────
    def test_unresolved_rows_excluded(self):
        rows = (
            # 5 resolved wins
            [{"ticker": f"R{i}", "scan_date": f"2026-01-{i+2:02d}",
              "oc_status": "READY", "r_multiple": 2.0,
              "resolved": "True", "final_outcome": "T1_HIT"}
             for i in range(5)]
            +
            # 3 resolved losses
            [{"ticker": f"RL{i}", "scan_date": f"2026-01-{i+10:02d}",
              "oc_status": "READY", "r_multiple": -1.0,
              "resolved": "True", "final_outcome": "STOP_HIT"}
             for i in range(3)]
            +
            # 10 unresolved — should NOT count
            [{"ticker": f"U{i}", "scan_date": f"2026-02-{i+2:02d}",
              "oc_status": "READY", "r_multiple": "",
              "resolved": "False", "final_outcome": "OPEN"}
             for i in range(10)]
        )
        df    = _make_opp_log(rows)
        stats = compute_opportunity_stats(df)
        self.assertEqual(stats["READY"]["n"], 8)

    # ── Infinite PF when no losses ─────────────────────────────────────────
    def test_infinite_pf_when_no_losses(self):
        rows = [
            {"ticker": f"W{i}", "scan_date": f"2026-01-{i+2:02d}",
             "oc_status": "WATCH", "r_multiple": 2.0,
             "resolved": "True", "final_outcome": "T1_HIT"}
            for i in range(6)
        ]
        df    = _make_opp_log(rows)
        stats = compute_opportunity_stats(df)
        self.assertEqual(stats["WATCH"]["profit_factor"], float("inf"))

    # ── avg_mfe / avg_mae ──────────────────────────────────────────────────
    def test_avg_mfe_computed(self):
        s = self.stats["READY"]
        self.assertIsNotNone(s["avg_mfe"])
        self.assertAlmostEqual(s["avg_mfe"], 5.0 * 5 / 8 + 2.0 * 3 / 8, places=1)

    def test_avg_mae_computed(self):
        s = self.stats["READY"]
        self.assertIsNotNone(s["avg_mae"])


# ═══════════════════════════════════════════════════
# 5. load_opportunity_log — File Handling
# ═══════════════════════════════════════════════════
class TestLoadOpportunityLog(unittest.TestCase):

    def test_missing_file_returns_empty_df(self):
        with tempfile.TemporaryDirectory() as tmp:
            df = load_opportunity_log(tmp)
        self.assertIsInstance(df, pd.DataFrame)
        self.assertTrue(df.empty)
        self.assertListEqual(list(df.columns), OPP_COLUMNS)

    def test_empty_file_returns_empty_df(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opportunity_log.csv"
            path.write_text("")
            df = load_opportunity_log(tmp)
        self.assertTrue(df.empty)

    def test_loads_existing_file(self):
        rows = [
            {"ticker": "AAA.NS", "scan_date": "2026-01-02",
             "oc_status": "READY", "r_multiple": 2.0,
             "resolved": "True", "final_outcome": "T1_HIT",
             "mfe_pct": 5.0, "mae_pct": -2.0},
        ]
        df_in = _make_opp_log(rows)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opportunity_log.csv"
            df_in.to_csv(path, index=False)
            df_out = load_opportunity_log(tmp)
        self.assertEqual(len(df_out), 1)
        self.assertEqual(df_out.iloc[0]["ticker"], "AAA.NS")
        self.assertAlmostEqual(df_out.iloc[0]["r_multiple"], 2.0)

    def test_numeric_columns_coerced(self):
        rows = [
            {"ticker": "BBB.NS", "scan_date": "2026-01-03",
             "oc_status": "REJECTED", "r_multiple": "-1.0",
             "resolved": "True", "final_outcome": "STOP_HIT",
             "score": "55", "mfe_pct": "3.0", "mae_pct": "-4.5"},
        ]
        df_in = _make_opp_log(rows)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opportunity_log.csv"
            df_in.to_csv(path, index=False)
            df_out = load_opportunity_log(tmp)
        self.assertEqual(df_out["r_multiple"].dtype, float)
        self.assertAlmostEqual(df_out.iloc[0]["mfe_pct"], 3.0)


# ═══════════════════════════════════════════════════
# 6. resolve_opportunity_outcomes — Integration (mocked)
# ═══════════════════════════════════════════════════
class TestResolveOpportunityOutcomes(unittest.TestCase):
    """Integration test with mocked yfinance and real filesystem."""

    def _make_scan_log(self) -> pd.DataFrame:
        """3 tickers: READY, WATCH, AVOID — one scan each."""
        return pd.DataFrame([
            {
                "scan_timestamp": "2026-01-02 09:30:00",
                "scan_date": "2026-01-02",
                "ticker": "READY.NS",
                "status": "READY",
                "score": 75,
                "entry_price": 100.0,
                "stop_price": 95.0,
                "t1": 110.0,
                "t2": 115.0,
                "rr_t1": 2.0,
            },
            {
                "scan_timestamp": "2026-01-02 09:30:00",
                "scan_date": "2026-01-02",
                "ticker": "WATCH.NS",
                "status": "WATCH",
                "score": 60,
                "entry_price": 200.0,
                "stop_price": 190.0,
                "t1": 220.0,
                "t2": 230.0,
                "rr_t1": 2.0,
            },
            {
                "scan_timestamp": "2026-01-02 09:30:00",
                "scan_date": "2026-01-02",
                "ticker": "AVOID.NS",
                "status": "AVOID",
                "score": 30,
                "entry_price": 300.0,
                "stop_price": 285.0,
                "t1": 330.0,
                "t2": 345.0,
                "rr_t1": 2.0,
            },
        ])

    def test_all_three_buckets_resolved(self):
        def fake_fetch(ticker, start_date, max_days=60):
            # Scan-day bar + one resolution bar that hits T1
            entry = {"READY.NS": 100, "WATCH.NS": 200, "AVOID.NS": 300}[ticker]
            t1    = {"READY.NS": 110, "WATCH.NS": 220, "AVOID.NS": 330}[ticker]
            stop  = {"READY.NS":  95, "WATCH.NS": 190, "AVOID.NS": 285}[ticker]
            return _make_ohlcv_df([
                (entry, entry*1.01, entry*0.99, entry, 1000),        # scan day
                (entry, t1*1.01,   stop*1.01,  t1,    1000),        # T1 hit
            ])

        scan_df = self._make_scan_log()

        with tempfile.TemporaryDirectory() as tmp:
            with patch("analytics.opportunity_tracker._fetch_prices", side_effect=fake_fetch), \
                 patch("analytics.opportunity_tracker.load_scan_log", return_value=scan_df):
                from analytics.opportunity_tracker import resolve_opportunity_outcomes
                summary = resolve_opportunity_outcomes(journal_dir=tmp)

        self.assertEqual(summary["resolved"], 3)
        self.assertEqual(summary["errors"], 0)

    def test_skip_already_resolved(self):
        # Pre-populate log with one resolved row
        existing = _make_opp_log([{
            "ticker": "READY.NS",
            "scan_date": "2026-01-02",
            "oc_status": "READY",
            "r_multiple": 2.0,
            "resolved": "True",
            "final_outcome": "T1_HIT",
        }])

        def fake_fetch(ticker, start_date, max_days=60):
            entry = {"WATCH.NS": 200, "AVOID.NS": 300}[ticker]
            t1    = {"WATCH.NS": 220, "AVOID.NS": 330}[ticker]
            stop  = {"WATCH.NS": 190, "AVOID.NS": 285}[ticker]
            return _make_ohlcv_df([
                (entry, entry*1.01, entry*0.99, entry, 1000),
                (entry, t1*1.01,   stop*1.01,  t1,    1000),
            ])

        scan_df = self._make_scan_log()

        with tempfile.TemporaryDirectory() as tmp:
            existing.to_csv(Path(tmp) / "opportunity_log.csv", index=False)
            with patch("analytics.opportunity_tracker._fetch_prices", side_effect=fake_fetch), \
                 patch("analytics.opportunity_tracker.load_scan_log", return_value=scan_df):
                from analytics.opportunity_tracker import resolve_opportunity_outcomes
                summary = resolve_opportunity_outcomes(journal_dir=tmp)

        # READY.NS already resolved → skipped; WATCH.NS + AVOID.NS processed
        self.assertEqual(summary["resolved"], 2)
        self.assertEqual(summary["skipped"],  1)

    def test_empty_scan_log_returns_zero_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("analytics.opportunity_tracker.load_scan_log",
                       return_value=pd.DataFrame()):
                from analytics.opportunity_tracker import resolve_opportunity_outcomes
                summary = resolve_opportunity_outcomes(journal_dir=tmp)
        self.assertEqual(summary["resolved"], 0)


# ═══════════════════════════════════════════════════
# 7. End-to-End: Sample Historical Data Scenario
# ═══════════════════════════════════════════════════
class TestHistoricalScenario(unittest.TestCase):
    """
    Verifies the complete pipeline on a synthetic historical dataset
    designed to show a filter-hurting scenario.

    Setup:
      20 trading days, 3 tickers per day:
        - BULL.NS  → always READY   → 65% win rate, avg win +2R
        - MID.NS   → always WATCH   → 50% win rate, avg win +2R
        - SLOW.NS  → always AVOID   → 75% win rate, avg win +2R
                        (SLOW is "better" — tests filter-hurting detection)

    Expected: REJECTED PF > READY PF
    """

    def _build_synthetic_log(self, n_days=20):
        rows = []
        import random
        rng = random.Random(42)

        for d in range(n_days):
            date_str = f"2026-01-{d+2:02d}" if d < 29 else f"2026-02-{d-28:02d}"

            # READY ticker: 65% win
            win_ready = rng.random() < 0.65
            rows.append({
                "ticker": "BULL.NS", "scan_date": date_str,
                "oc_status": "READY", "raw_status": "READY",
                "r_multiple": 2.0 if win_ready else -1.0,
                "mfe_pct": 6.0 if win_ready else 1.5,
                "mae_pct": -1.5 if win_ready else -5.5,
                "resolved": "True",
                "final_outcome": "T1_HIT" if win_ready else "STOP_HIT",
            })

            # WATCH ticker: 50% win
            win_watch = rng.random() < 0.50
            rows.append({
                "ticker": "MID.NS", "scan_date": date_str,
                "oc_status": "WATCH", "raw_status": "WATCH",
                "r_multiple": 2.0 if win_watch else -1.0,
                "mfe_pct": 5.0 if win_watch else 2.0,
                "mae_pct": -1.0 if win_watch else -4.0,
                "resolved": "True",
                "final_outcome": "T1_HIT" if win_watch else "STOP_HIT",
            })

            # REJECTED ticker: 75% win (should produce higher PF than READY)
            win_rej = rng.random() < 0.75
            rows.append({
                "ticker": "SLOW.NS", "scan_date": date_str,
                "oc_status": "REJECTED", "raw_status": "AVOID",
                "r_multiple": 2.0 if win_rej else -1.0,
                "mfe_pct": 7.0 if win_rej else 1.0,
                "mae_pct": -0.5 if win_rej else -5.0,
                "resolved": "True",
                "final_outcome": "T1_HIT" if win_rej else "STOP_HIT",
            })

        return _make_opp_log(rows)

    def test_rejected_pf_greater_than_ready_pf(self):
        df    = self._build_synthetic_log(n_days=20)
        stats = compute_opportunity_stats(df)
        self.assertIsNotNone(stats["REJECTED"],
                             "REJECTED bucket should have enough data")
        self.assertIsNotNone(stats["READY"],
                             "READY bucket should have enough data")
        self.assertGreater(stats["REJECTED"]["profit_factor"],
                           stats["READY"]["profit_factor"],
                           "Synthetic REJECTED (75% WR) should outperform READY (65% WR)")

    def test_all_buckets_present(self):
        df    = self._build_synthetic_log(n_days=20)
        stats = compute_opportunity_stats(df)
        for bucket in ("READY", "WATCH", "REJECTED", "ALL"):
            self.assertIsNotNone(stats[bucket],
                                 f"{bucket} should have ≥{MIN_SAMPLE} samples")

    def test_all_n_equals_n_days(self):
        n = 20
        df    = self._build_synthetic_log(n_days=n)
        stats = compute_opportunity_stats(df)
        self.assertEqual(stats["READY"]["n"],    n)
        self.assertEqual(stats["WATCH"]["n"],    n)
        self.assertEqual(stats["REJECTED"]["n"], n)
        self.assertEqual(stats["ALL"]["n"],      n * 3)

    def test_expectancy_and_pf_consistent(self):
        """PF > 1 ↔ expectancy > 0 for symmetric R:R."""
        df    = self._build_synthetic_log(n_days=20)
        stats = compute_opportunity_stats(df)
        for bucket in ("READY", "WATCH", "REJECTED"):
            s = stats[bucket]
            if s is None:
                continue
            pf_positive  = s["profit_factor"] > 1.0
            exp_positive = s["expectancy_r"] > 0.0
            self.assertEqual(pf_positive, exp_positive,
                             f"{bucket}: PF and expectancy should agree in sign")


if __name__ == "__main__":
    unittest.main(verbosity=2)