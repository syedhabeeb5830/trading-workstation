"""
tests/test_setup_store.py — Setup Lifecycle Manager Tests
=========================================================
Fully offline — no yfinance calls, no external dependencies.
All data is synthetic. Tests use tempfile for file I/O isolation.

Coverage:
  1. upsert_setup — new setup creation
  2. upsert_setup — deduplication (same strategy/ticker updates existing)
  3. upsert_setup — status upgrade WATCH→READY, no downgrade
  4. upsert_setup — REJECTED closes active setup
  5. expire_stale_setups — marks setups EXPIRED after N trading days
  6. expire_stale_setups — does NOT expire setups within expiry window
  7. mark_triggered — transitions READY→TRIGGERED, removes from active index
  8. mark_closed — transitions TRIGGERED→CLOSED with resolution
  9. get_active_setups — only returns WATCH/READY
  10. save / load roundtrip — persistence via JSON
  11. dedup_count increments correctly
  12. get_lifecycle_stats — correct aggregates
  13. Opportunity tracker: _build_candidate_index deduplicates by setup
  14. Opportunity tracker: dedup does not change metrics when same setup re-scanned
  15. _trading_days_between helper
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from analytics.setup_store import (
    SetupStore,
    _trading_days_between,
    DEFAULT_EXPIRY_DAYS,
    ACTIVE_STATUSES,
)
from analytics.opportunity_tracker import (
    _build_candidate_index,
    _map_oc_status,
    compute_opportunity_stats,
    OPP_COLUMNS,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _store(tmp: str) -> SetupStore:
    return SetupStore(tmp)


def _upsert(store, ticker="SBIN.NS", strategy="SWING",
            entry=500.0, stop=490.0,
            scan_status="WATCH", scan_date="2026-06-02") -> tuple:
    return store.upsert_setup(ticker, strategy, entry, stop, scan_status, scan_date)


def _make_scan_df(rows: list[dict]) -> pd.DataFrame:
    """Create a minimal scan_log DataFrame from a list of row dicts."""
    defaults = {
        "scan_timestamp": "2026-06-02 09:30:00",
        "scan_date":      "2026-06-02",
        "ticker":         "TEST.NS",
        "status":         "WATCH",
        "entry_price":    100.0,
        "stop_price":     95.0,
        "t1":             110.0,
        "t2":             115.0,
        "rr_t1":          2.0,
        "score":          70,
    }
    records = []
    for r in rows:
        row = dict(defaults)
        row.update(r)
        records.append(row)
    return pd.DataFrame(records)


# ─────────────────────────────────────────────────────────────────────────────
# 1–2. Basic upsert
# ─────────────────────────────────────────────────────────────────────────────

class TestUpsertSetupNew(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_creates_new_setup(self):
        store = _store(self.tmpdir)
        sid, created = _upsert(store)
        self.assertTrue(created, "First upsert should create a new setup")
        self.assertIn(sid, store._setups)

    def test_setup_fields_populated(self):
        store = _store(self.tmpdir)
        sid, _ = _upsert(store, ticker="RELIANCE.NS", entry=2500.0, stop=2450.0,
                          scan_status="READY", scan_date="2026-06-03")
        s = store.get_setup(sid)
        self.assertEqual(s["ticker"],     "RELIANCE.NS")
        self.assertEqual(s["strategy"],   "SWING")
        self.assertEqual(s["first_seen"], "2026-06-03")
        self.assertEqual(s["last_seen"],  "2026-06-03")
        self.assertEqual(s["status"],     "READY")
        self.assertEqual(s["entry"],      2500.0)
        self.assertEqual(s["stop"],       2450.0)
        self.assertEqual(s["days_active"], 1)

    def test_active_index_populated(self):
        store = _store(self.tmpdir)
        sid, _ = _upsert(store)
        key = "SWING|SBIN.NS"
        self.assertIn(key, store._active_index)
        self.assertEqual(store._active_index[key], sid)


class TestUpsertSetupDedup(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_second_scan_updates_not_creates(self):
        store = _store(self.tmpdir)
        sid1, c1 = _upsert(store, scan_date="2026-06-02", scan_status="WATCH")
        sid2, c2 = _upsert(store, scan_date="2026-06-03", scan_status="WATCH")
        self.assertTrue(c1)
        self.assertFalse(c2, "Second scan of same setup should update, not create")
        self.assertEqual(sid1, sid2, "setup_id must not change on update")
        self.assertEqual(len(store._setups), 1)

    def test_dedup_count_increments(self):
        store = _store(self.tmpdir)
        _upsert(store, scan_date="2026-06-02")
        _upsert(store, scan_date="2026-06-03")
        _upsert(store, scan_date="2026-06-04")
        self.assertEqual(store._dedup_count, 2)

    def test_last_seen_updated(self):
        store = _store(self.tmpdir)
        sid, _ = _upsert(store, scan_date="2026-06-02")
        _upsert(store, scan_date="2026-06-05")
        s = store.get_setup(sid)
        self.assertEqual(s["last_seen"], "2026-06-05")

    def test_entry_stop_updated_on_refresh(self):
        store = _store(self.tmpdir)
        sid, _ = _upsert(store, entry=500.0, stop=490.0, scan_date="2026-06-02")
        _upsert(store, entry=505.0, stop=493.0, scan_date="2026-06-03")
        s = store.get_setup(sid)
        self.assertEqual(s["entry"], 505.0)
        self.assertEqual(s["stop"],  493.0)

    def test_different_tickers_create_separate_setups(self):
        store = _store(self.tmpdir)
        _upsert(store, ticker="SBIN.NS",     scan_date="2026-06-02")
        _upsert(store, ticker="RELIANCE.NS", scan_date="2026-06-02")
        self.assertEqual(len(store._setups), 2)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Status transitions
# ─────────────────────────────────────────────────────────────────────────────

class TestStatusTransitions(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_watch_upgrades_to_ready(self):
        store = _store(self.tmpdir)
        sid, _ = _upsert(store, scan_status="WATCH", scan_date="2026-06-02")
        _upsert(store, scan_status="READY", scan_date="2026-06-03")
        self.assertEqual(store.get_setup(sid)["status"], "READY")

    def test_ready_does_not_downgrade_to_watch(self):
        store = _store(self.tmpdir)
        sid, _ = _upsert(store, scan_status="READY", scan_date="2026-06-02")
        _upsert(store, scan_status="WATCH", scan_date="2026-06-03")
        self.assertEqual(store.get_setup(sid)["status"], "READY",
                         "READY must never downgrade to WATCH")

    def test_extended_maps_to_watch(self):
        store = _store(self.tmpdir)
        sid, _ = _upsert(store, scan_status="WATCH", scan_date="2026-06-02")
        _upsert(store, scan_status="EXTENDED", scan_date="2026-06-03")
        self.assertEqual(store.get_setup(sid)["status"], "WATCH",
                         "EXTENDED should keep setup in WATCH (same setup, price ran past)")

    def test_rejected_closes_active_setup(self):
        store = _store(self.tmpdir)
        sid, _ = _upsert(store, scan_status="WATCH", scan_date="2026-06-02")
        _upsert(store, scan_status="AVOID", scan_date="2026-06-03")
        s = store.get_setup(sid)
        self.assertEqual(s["status"], "REJECTED")
        key = "SWING|SBIN.NS"
        self.assertNotIn(key, store._active_index,
                         "REJECTED setup must be removed from active index")

    def test_rejected_ticker_new_scan_creates_new_setup(self):
        store = _store(self.tmpdir)
        sid1, _ = _upsert(store, scan_status="WATCH", scan_date="2026-06-02")
        _upsert(store, scan_status="AVOID", scan_date="2026-06-03")
        # Next day it appears as WATCH again → new setup
        sid2, created = _upsert(store, scan_status="WATCH", scan_date="2026-06-05")
        self.assertTrue(created)
        self.assertNotEqual(sid1, sid2)


# ─────────────────────────────────────────────────────────────────────────────
# 5–6. Expiry
# ─────────────────────────────────────────────────────────────────────────────

class TestExpiry(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_expired_after_expiry_days(self):
        store = _store(self.tmpdir)
        sid, _ = _upsert(store, scan_status="WATCH", scan_date="2026-05-26")
        # 2026-06-05 is 8 trading days after 2026-05-26
        expired = store.expire_stale_setups("2026-06-05", expiry_days=7)
        self.assertIn(sid, expired)
        self.assertEqual(store.get_setup(sid)["status"], "EXPIRED")

    def test_not_expired_within_window(self):
        store = _store(self.tmpdir)
        sid, _ = _upsert(store, scan_status="WATCH", scan_date="2026-06-03")
        # Only 2 trading days later — should not expire
        expired = store.expire_stale_setups("2026-06-05", expiry_days=7)
        self.assertNotIn(sid, expired)
        self.assertEqual(store.get_setup(sid)["status"], "WATCH")

    def test_expired_removed_from_active_index(self):
        store = _store(self.tmpdir)
        _upsert(store, scan_status="WATCH", scan_date="2026-05-20")
        store.expire_stale_setups("2026-06-05", expiry_days=7)
        key = "SWING|SBIN.NS"
        self.assertNotIn(key, store._active_index)

    def test_triggered_setup_not_expired(self):
        store = _store(self.tmpdir)
        sid, _ = _upsert(store, scan_status="READY", scan_date="2026-05-20")
        store.mark_triggered("SBIN.NS")
        expired = store.expire_stale_setups("2026-06-10", expiry_days=7)
        self.assertNotIn(sid, expired,
                         "TRIGGERED setups must never be expired")


# ─────────────────────────────────────────────────────────────────────────────
# 7–8. mark_triggered / mark_closed
# ─────────────────────────────────────────────────────────────────────────────

class TestMarkTriggeredClosed(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_mark_triggered(self):
        store = _store(self.tmpdir)
        sid, _ = _upsert(store, scan_status="READY", scan_date="2026-06-02")
        result = store.mark_triggered("SBIN.NS")
        self.assertEqual(result, sid)
        self.assertEqual(store.get_setup(sid)["status"], "TRIGGERED")

    def test_mark_triggered_removes_from_active(self):
        store = _store(self.tmpdir)
        _upsert(store, scan_status="READY", scan_date="2026-06-02")
        store.mark_triggered("SBIN.NS")
        key = "SWING|SBIN.NS"
        self.assertNotIn(key, store._active_index)

    def test_mark_triggered_unknown_ticker_returns_none(self):
        store = _store(self.tmpdir)
        result = store.mark_triggered("UNKNOWN.NS")
        self.assertIsNone(result)

    def test_mark_closed(self):
        store = _store(self.tmpdir)
        sid, _ = _upsert(store, scan_status="READY", scan_date="2026-06-02")
        store.mark_triggered("SBIN.NS")
        store.mark_closed(sid, "T1_HIT")
        s = store.get_setup(sid)
        self.assertEqual(s["status"],     "CLOSED")
        self.assertEqual(s["resolution"], "T1_HIT")


# ─────────────────────────────────────────────────────────────────────────────
# 9. get_active_setups
# ─────────────────────────────────────────────────────────────────────────────

class TestGetActiveSetups(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_returns_only_active(self):
        store = _store(self.tmpdir)
        _upsert(store, ticker="SBIN.NS",     scan_status="WATCH",    scan_date="2026-06-02")
        _upsert(store, ticker="RELIANCE.NS", scan_status="READY",    scan_date="2026-06-02")
        _upsert(store, ticker="INFOSYS.NS",  scan_status="AVOID",    scan_date="2026-06-02")
        active = store.get_active_setups()
        tickers = {s["ticker"] for s in active}
        self.assertIn("SBIN.NS",     tickers)
        self.assertIn("RELIANCE.NS", tickers)
        self.assertNotIn("INFOSYS.NS", tickers)

    def test_active_count_after_expire(self):
        store = _store(self.tmpdir)
        _upsert(store, ticker="SBIN.NS", scan_status="WATCH", scan_date="2026-05-20")
        store.expire_stale_setups("2026-06-10", expiry_days=7)
        active = store.get_active_setups()
        self.assertEqual(len(active), 0)


# ─────────────────────────────────────────────────────────────────────────────
# 10. Save / load roundtrip
# ─────────────────────────────────────────────────────────────────────────────

class TestPersistence(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_save_and_reload(self):
        store = _store(self.tmpdir)
        sid, _ = _upsert(store, ticker="TITAN.NS", scan_status="READY", scan_date="2026-06-02")
        _upsert(store, ticker="TITAN.NS", scan_status="READY", scan_date="2026-06-03")
        store.save()

        store2 = _store(self.tmpdir)
        self.assertEqual(len(store2._setups), 1)
        self.assertEqual(store2.get_setup(sid)["status"], "READY")
        self.assertEqual(store2._dedup_count, 1)

    def test_active_index_rebuilt_on_load(self):
        store = _store(self.tmpdir)
        _upsert(store, ticker="TITAN.NS", scan_status="WATCH", scan_date="2026-06-02")
        store.save()

        store2 = _store(self.tmpdir)
        key = "SWING|TITAN.NS"
        self.assertIn(key, store2._active_index)


# ─────────────────────────────────────────────────────────────────────────────
# 12. get_lifecycle_stats
# ─────────────────────────────────────────────────────────────────────────────

class TestLifecycleStats(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def test_counts_by_status(self):
        store = _store(self.tmpdir)
        # A.NS started early — will be expired on 2026-06-15 (>7 trading days)
        _upsert(store, ticker="A.NS", scan_status="WATCH",  scan_date="2026-06-02")
        # B.NS started recently — still within the 7-day window on 2026-06-15
        _upsert(store, ticker="B.NS", scan_status="READY",  scan_date="2026-06-13")
        # C.NS was AVOID → REJECTED immediately
        _upsert(store, ticker="C.NS", scan_status="AVOID",  scan_date="2026-06-02")
        store.expire_stale_setups("2026-06-15", expiry_days=7)

        stats = store.get_lifecycle_stats()
        self.assertEqual(stats["active"],  1)   # B is still READY (started 2026-06-13)
        self.assertEqual(stats["rejected"],1)   # C is REJECTED
        self.assertEqual(stats["expired"], 1)   # A expired

    def test_dedup_count_reflected(self):
        store = _store(self.tmpdir)
        _upsert(store, ticker="A.NS", scan_date="2026-06-02")
        _upsert(store, ticker="A.NS", scan_date="2026-06-03")
        _upsert(store, ticker="A.NS", scan_date="2026-06-04")
        stats = store.get_lifecycle_stats()
        self.assertEqual(stats["dedup_prevented"], 2)

    def test_avg_duration_single_setup(self):
        store = _store(self.tmpdir)
        _upsert(store, ticker="A.NS", scan_date="2026-06-02")
        stats = store.get_lifecycle_stats()
        self.assertGreaterEqual(stats["avg_duration_days"], 1.0)


# ─────────────────────────────────────────────────────────────────────────────
# 13–14. Opportunity tracker deduplication
# ─────────────────────────────────────────────────────────────────────────────

class TestOpportunityTrackerDedup(unittest.TestCase):

    def test_multiple_scans_same_ticker_produce_one_candidate(self):
        """5 daily scans of the same ticker → 1 unique setup candidate."""
        rows = [
            {"ticker": "SBIN.NS", "scan_date": f"2026-06-0{i}",
             "scan_timestamp": f"2026-06-0{i} 09:30:00",
             "status": "WATCH", "entry_price": 500.0, "stop_price": 490.0,
             "t1": 520.0, "t2": 530.0, "rr_t1": 2.0, "score": 70}
            for i in range(2, 7)
        ]
        df = _make_scan_df(rows)
        candidates = _build_candidate_index(df)
        sbin_rows = candidates[candidates["ticker"] == "SBIN.NS"]
        self.assertEqual(len(sbin_rows), 1,
                         "5 daily scans of same ticker must produce exactly 1 candidate setup")

    def test_two_different_tickers_produce_two_candidates(self):
        rows = [
            {"ticker": "SBIN.NS",     "scan_date": "2026-06-02",
             "scan_timestamp": "2026-06-02 09:30:00",
             "status": "READY", "entry_price": 500.0, "stop_price": 490.0,
             "t1": 520.0, "t2": 530.0, "rr_t1": 2.0, "score": 75},
            {"ticker": "RELIANCE.NS", "scan_date": "2026-06-02",
             "scan_timestamp": "2026-06-02 09:30:00",
             "status": "WATCH", "entry_price": 2500.0, "stop_price": 2450.0,
             "t1": 2600.0, "t2": 2650.0, "rr_t1": 2.0, "score": 65},
        ]
        df = _make_scan_df(rows)
        candidates = _build_candidate_index(df)
        self.assertEqual(len(candidates), 2)

    def test_first_scan_status_is_used_for_oc_status(self):
        """When WATCH then READY, oc_status should reflect WATCH (first_seen status)."""
        rows = [
            {"ticker": "TITAN.NS", "scan_date": "2026-06-02",
             "scan_timestamp": "2026-06-02 09:30:00",
             "status": "WATCH", "entry_price": 3000.0, "stop_price": 2950.0,
             "t1": 3100.0, "t2": 3150.0, "rr_t1": 2.0, "score": 65},
            {"ticker": "TITAN.NS", "scan_date": "2026-06-03",
             "scan_timestamp": "2026-06-03 09:30:00",
             "status": "READY", "entry_price": 3005.0, "stop_price": 2955.0,
             "t1": 3105.0, "t2": 3155.0, "rr_t1": 2.0, "score": 68},
        ]
        df = _make_scan_df(rows)
        candidates = _build_candidate_index(df)
        row = candidates[candidates["ticker"] == "TITAN.NS"].iloc[0]
        self.assertEqual(row["raw_status"], "WATCH",
                         "raw_status should be from first scan")
        self.assertEqual(row["oc_status"], "WATCH")

    def test_setup_id_present_in_candidates(self):
        rows = [
            {"ticker": "SBIN.NS", "scan_date": "2026-06-02",
             "scan_timestamp": "2026-06-02 09:30:00",
             "status": "READY", "entry_price": 500.0, "stop_price": 490.0,
             "t1": 520.0, "t2": 530.0, "rr_t1": 2.0, "score": 72},
        ]
        df = _make_scan_df(rows)
        candidates = _build_candidate_index(df)
        self.assertIn("setup_id",   candidates.columns)
        self.assertIn("first_seen", candidates.columns)
        self.assertIn("last_seen",  candidates.columns)
        sid = candidates.iloc[0]["setup_id"]
        self.assertTrue(len(str(sid)) > 5, "setup_id should be a non-trivial string")


# ─────────────────────────────────────────────────────────────────────────────
# Stats: unique setup counts feed into compute_opportunity_stats
# ─────────────────────────────────────────────────────────────────────────────

class TestOpportunityStatsUniqueness(unittest.TestCase):

    def _make_resolved_df(self, rows: list[dict]) -> pd.DataFrame:
        defaults = {
            "setup_id":     "SETUP_1",
            "ticker":       "TEST.NS",
            "strategy":     "SWING",
            "first_seen":   "2026-06-02",
            "last_seen":    "2026-06-05",
            "days_active":  3,
            "score":        70,
            "raw_status":   "READY",
            "oc_status":    "READY",
            "entry_price":  100.0,
            "stop_price":   95.0,
            "t1":           110.0,
            "t2":           115.0,
            "rr_t1":        2.0,
            "mfe_pct":      5.0,
            "mae_pct":      -1.5,
            "r_multiple":   2.0,
            "final_outcome": "T1_HIT",
            "resolved":     "True",
        }
        records = []
        for i, r in enumerate(rows):
            row = dict(defaults)
            row["setup_id"] = f"SETUP_{i+1}"
            row.update(r)
            records.append(row)
        return pd.DataFrame(records, columns=OPP_COLUMNS)

    def test_stats_require_min_sample(self):
        """Bucket with < MIN_SAMPLE returns None."""
        df = self._make_resolved_df([
            {"oc_status": "READY", "r_multiple": 2.0},
            {"oc_status": "READY", "r_multiple": -1.0},
        ])
        stats = compute_opportunity_stats(df)
        self.assertIsNone(stats["READY"], "Need >= 5 setups for meaningful stats")

    def test_stats_expectancy_calculation(self):
        """5 READY setups: 3W@+2R + 2L@-1R → expectancy=(6-2)/5=+0.8R."""
        rows = [
            {"oc_status": "READY", "r_multiple": 2.0},
            {"oc_status": "READY", "r_multiple": 2.0},
            {"oc_status": "READY", "r_multiple": 2.0},
            {"oc_status": "READY", "r_multiple": -1.0},
            {"oc_status": "READY", "r_multiple": -1.0},
        ]
        df = self._make_resolved_df(rows)
        stats = compute_opportunity_stats(df)
        r = stats["READY"]
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["expectancy_r"],  0.8, places=2)
        self.assertAlmostEqual(r["win_rate_pct"], 60.0, places=1)

    def test_each_row_is_unique_setup(self):
        """Stats count rows (setups), not observations."""
        rows = [{"oc_status": "REJECTED", "r_multiple": 2.0}] * 5
        df = self._make_resolved_df(rows)
        stats = compute_opportunity_stats(df)
        self.assertEqual(stats["REJECTED"]["n"], 5)


# ─────────────────────────────────────────────────────────────────────────────
# 15. _trading_days_between helper
# ─────────────────────────────────────────────────────────────────────────────

class TestTradingDays(unittest.TestCase):

    def test_same_day_is_one(self):
        self.assertEqual(_trading_days_between("2026-06-05", "2026-06-05"), 1)

    def test_consecutive_business_days(self):
        # Mon → Tue = 2 business days inclusive
        self.assertEqual(_trading_days_between("2026-06-01", "2026-06-02"), 2)

    def test_week_span(self):
        # Mon → Fri = 5 business days
        self.assertEqual(_trading_days_between("2026-06-01", "2026-06-05"), 5)

    def test_span_across_weekend(self):
        # Fri → Mon = 2 business days (Fri + Mon)
        self.assertEqual(_trading_days_between("2026-05-29", "2026-06-01"), 2)

    def test_invalid_date_returns_one(self):
        result = _trading_days_between("not-a-date", "2026-06-05")
        self.assertEqual(result, 1)


# ─────────────────────────────────────────────────────────────────────────────
# _map_oc_status (unchanged, verify it still works)
# ─────────────────────────────────────────────────────────────────────────────

class TestMapOcStatus(unittest.TestCase):

    def test_ready_maps_to_ready(self):
        self.assertEqual(_map_oc_status("READY"), "READY")

    def test_watch_maps_to_watch(self):
        self.assertEqual(_map_oc_status("WATCH"), "WATCH")

    def test_extended_maps_to_watch(self):
        self.assertEqual(_map_oc_status("EXTENDED"), "WATCH")

    def test_avoid_maps_to_rejected(self):
        self.assertEqual(_map_oc_status("AVOID"), "REJECTED")

    def test_gapped_maps_to_rejected(self):
        self.assertEqual(_map_oc_status("GAPPED"), "REJECTED")

    def test_case_insensitive(self):
        self.assertEqual(_map_oc_status("ready"), "READY")
        self.assertEqual(_map_oc_status("Watch"), "WATCH")


if __name__ == "__main__":
    unittest.main(verbosity=2)
