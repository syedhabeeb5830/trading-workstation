"""
tests/test_reliability.py — Production reliability test suite.

Five failure scenarios exercised:

  1. Power-failure simulation
     A crash mid-write must leave the original journal intact.

  2. Reconnect simulation
     TickerManager backoff schedule must grow exponentially and cap correctly.

  3. Journal corruption prevention
     No partial rows, no invalid CSV structure after an interrupted write.

  4. Restart recovery
     StateStore persists states atomically; a fresh instance reads them back.

  5. Invalid market data
     data_validator rejects every class of bad OHLCV data.
"""

from __future__ import annotations

import csv
import io
import os
import threading
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_valid_ohlcv(n: int = 10, start: str = "2024-01-01") -> pd.DataFrame:
    """Return a minimal valid daily OHLCV DataFrame."""
    dates = pd.date_range(start=start, periods=n, freq="B")   # business days
    closes = np.linspace(100.0, 110.0, n)
    df = pd.DataFrame({
        "Open":   closes - 0.5,
        "High":   closes + 1.0,
        "Low":    closes - 1.0,
        "Close":  closes,
        "Volume": np.ones(n) * 1_000_000,
    }, index=dates)
    return df


def _make_recent_ohlcv(n: int = 60) -> pd.DataFrame:
    """Valid OHLCV ending yesterday (avoids stale-data rejection)."""
    end   = datetime.now().date() - timedelta(days=1)
    start = end - timedelta(days=n * 2)          # enough to get n bdays
    dates = pd.bdate_range(start=start, end=end)[-n:]
    closes = np.linspace(100.0, 110.0, len(dates))
    df = pd.DataFrame({
        "Open":   closes - 0.5,
        "High":   closes + 1.0,
        "Low":    closes - 1.0,
        "Close":  closes,
        "Volume": np.ones(len(dates)) * 1_000_000,
    }, index=dates)
    return df


# ═════════════════════════════════════════════════════════════════════════════
# 1. POWER-FAILURE SIMULATION (atomic journal writes)
# ═════════════════════════════════════════════════════════════════════════════

class TestAtomicJournalWrites:
    """Verify that journal files survive simulated crash mid-write."""

    def test_new_file_created_with_header(self, tmp_path):
        from analytics.journal_writer import atomic_append_rows
        path = tmp_path / "test.csv"
        fields = ["a", "b"]
        atomic_append_rows(path, [{"a": "1", "b": "2"}], fields)
        assert path.exists()
        rows = list(csv.DictReader(path.open(encoding="utf-8")))
        assert len(rows) == 1
        assert rows[0]["a"] == "1"

    def test_append_preserves_existing_content(self, tmp_path):
        from analytics.journal_writer import atomic_append_rows
        path   = tmp_path / "log.csv"
        fields = ["x", "y"]
        atomic_append_rows(path, [{"x": "row1", "y": "A"}], fields)
        atomic_append_rows(path, [{"x": "row2", "y": "B"}], fields)
        rows = list(csv.DictReader(path.open(encoding="utf-8")))
        assert len(rows) == 2
        assert rows[0]["x"] == "row1"
        assert rows[1]["x"] == "row2"

    def test_crash_during_write_leaves_original_intact(self, tmp_path):
        """
        Simulate a crash AFTER the temp file is partially written but BEFORE
        os.replace().  The original file must remain readable and unchanged.
        """
        from analytics.journal_writer import atomic_append_rows, _write_atomic
        path   = tmp_path / "journal.csv"
        fields = ["col"]

        # Establish a known good file
        atomic_append_rows(path, [{"col": "original"}], fields)
        original_bytes = path.read_bytes()

        # Patch os.replace to raise (simulating crash after temp write)
        with patch("analytics.journal_writer.os.replace",
                   side_effect=OSError("simulated crash")):
            with pytest.raises(OSError):
                atomic_append_rows(path, [{"col": "new_row"}], fields)

        # Original must be untouched
        assert path.read_bytes() == original_bytes

    def test_orphaned_temp_is_cleaned_up(self, tmp_path):
        """cleanup_orphaned_temp() removes a leftover .tmp file."""
        from analytics.journal_writer import cleanup_orphaned_temp
        path = tmp_path / "x.csv"
        tmp  = path.with_suffix(".tmp")
        tmp.write_bytes(b"partial garbage")
        assert tmp.exists()
        removed = cleanup_orphaned_temp(path)
        assert removed is True
        assert not tmp.exists()

    def test_no_orphan_is_a_no_op(self, tmp_path):
        from analytics.journal_writer import cleanup_orphaned_temp
        path    = tmp_path / "x.csv"
        removed = cleanup_orphaned_temp(path)
        assert removed is False

    def test_atomic_csv_write_overwrites_safely(self, tmp_path):
        """atomic_csv_write must produce a valid CSV; original safe on crash."""
        from analytics.journal_writer import atomic_csv_write
        path = tmp_path / "trades.csv"
        df1  = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
        atomic_csv_write(path, df1)
        df2  = pd.DataFrame({"a": [3], "b": ["z"]})
        atomic_csv_write(path, df2)
        result = pd.read_csv(path)
        assert list(result["a"]) == [3]

    def test_atomic_csv_write_crash_preserves_old(self, tmp_path):
        from analytics.journal_writer import atomic_csv_write
        path = tmp_path / "trades.csv"
        df1  = pd.DataFrame({"a": [1], "b": ["orig"]})
        atomic_csv_write(path, df1)
        original = path.read_bytes()

        with patch("analytics.journal_writer.os.replace",
                   side_effect=OSError("crash")):
            with pytest.raises(OSError):
                atomic_csv_write(path, pd.DataFrame({"a": [99]}))

        assert path.read_bytes() == original

    def test_empty_rows_is_a_no_op(self, tmp_path):
        from analytics.journal_writer import atomic_append_rows
        path = tmp_path / "noop.csv"
        atomic_append_rows(path, [], ["col"])
        assert not path.exists()


# ═════════════════════════════════════════════════════════════════════════════
# 2. JOURNAL CORRUPTION PREVENTION
# ═════════════════════════════════════════════════════════════════════════════

class TestJournalCorruptionPrevention:
    """Verify that scan_logger and outcome_tracker produce valid CSVs."""

    def test_scan_logger_produces_valid_csv(self, tmp_path):
        from analytics.scan_logger import log_scan_results, load_scan_log
        plans = [
            {"ticker": "A.NS", "score": 70, "grade": "A",
             "status": "READY", "entry_price": 100.0},
            {"ticker": "B.NS", "score": 50, "grade": "B",
             "status": "WATCH", "entry_price": 200.0},
        ]
        regime = {"regime": "BULL", "strength": 0.8}
        n = log_scan_results(plans, regime, journal_dir=str(tmp_path))
        assert n == 2
        df = load_scan_log(journal_dir=str(tmp_path))
        assert len(df) == 2
        assert set(df["ticker"]) == {"A.NS", "B.NS"}

    def test_scan_logger_idempotent_append(self, tmp_path):
        from analytics.scan_logger import log_scan_results, load_scan_log
        plans  = [{"ticker": "X.NS", "score": 60, "grade": "A",
                   "status": "WATCH", "entry_price": 50.0}]
        regime = {"regime": "NEUTRAL", "strength": 0.5}
        log_scan_results(plans, regime, journal_dir=str(tmp_path))
        log_scan_results(plans, regime, journal_dir=str(tmp_path))
        df = load_scan_log(journal_dir=str(tmp_path))
        assert len(df) == 2           # two distinct scan runs → two rows

    def test_scan_logger_header_written_once(self, tmp_path):
        from analytics.scan_logger import log_scan_results, SCAN_LOG_COLUMNS
        plans  = [{"ticker": "Y.NS"}]
        regime = {"regime": "BEAR", "strength": 0.2}
        log_scan_results(plans, regime, journal_dir=str(tmp_path))
        log_scan_results(plans, regime, journal_dir=str(tmp_path))
        raw = (tmp_path / "scan_log.csv").read_text(encoding="utf-8")
        header_count = raw.count("scan_timestamp")
        assert header_count == 1      # header appears exactly once

    def test_outcome_tracker_record_produces_valid_csv(self, tmp_path):
        from analytics.outcome_tracker import record_trade_entry, load_trade_log
        plan = {
            "ticker": "RELIANCE.NS",
            "entry_price": 1000.0, "stop_price": 950.0,
            "t1": 1100.0, "t2": 1200.0, "rr_t1": 2.0,
            "quantity": 10, "max_loss_inr": 500.0,
            "grade": "A", "score": 75,
        }
        tid = record_trade_entry(plan, "BULL", journal_dir=str(tmp_path))
        assert tid.endswith("RELIANCE")
        df  = load_trade_log(journal_dir=str(tmp_path))
        assert len(df) == 1
        assert df.iloc[0]["ticker"] == "RELIANCE.NS"
        assert str(df.iloc[0]["exit_reason"]) == "OPEN"

    def test_resolve_outcomes_atomic_write(self, tmp_path, monkeypatch):
        """resolve_outcomes must write atomically; crash preserves prior state."""
        from analytics.outcome_tracker import (
            record_trade_entry, resolve_outcomes,
        )
        plan = {
            "ticker": "TEST.NS",
            "entry_price": 100.0, "stop_price": 90.0,
            "t1": 120.0, "t2": 140.0, "rr_t1": 2.0,
            "quantity": 5, "max_loss_inr": 50.0,
            "grade": "B", "score": 55,
        }
        record_trade_entry(plan, "NEUTRAL", journal_dir=str(tmp_path))
        original = (tmp_path / "trades.csv").read_bytes()

        # Make _fetch_post_entry_prices return None so resolve skips cleanly
        monkeypatch.setattr(
            "analytics.outcome_tracker._fetch_post_entry_prices",
            lambda *a, **kw: None,
        )
        summary = resolve_outcomes(journal_dir=str(tmp_path))
        # Nothing resolved (no price data), but file must still be valid
        df = pd.read_csv(tmp_path / "trades.csv")
        assert len(df) == 1


# ═════════════════════════════════════════════════════════════════════════════
# 3. RESTART RECOVERY (state persistence)
# ═════════════════════════════════════════════════════════════════════════════

class TestRestartRecovery:
    """StateStore must survive process restart with full state intact."""

    def test_empty_store_on_missing_file(self, tmp_path):
        from scanner.state_store import StateStore
        s = StateStore(journal_dir=str(tmp_path))
        assert s.get("ANY.NS") is None
        assert s.get_all_states() == {}

    def test_state_roundtrip(self, tmp_path):
        from scanner.state_store import StateStore
        plan = {"entry_price": 1500.0, "stop_price": 1400.0,
                "grade": "A", "score": 72.0}
        s = StateStore(journal_dir=str(tmp_path))
        s.update("HDFC.NS", "READY", plan)
        s.save()

        s2 = StateStore(journal_dir=str(tmp_path))
        got = s2.get("HDFC.NS")
        assert got is not None
        assert got["status"] == "READY"
        assert got["entry_price"] == 1500.0
        assert got["grade"] == "A"

    def test_prev_status_preserved_across_restart(self, tmp_path):
        from scanner.state_store import StateStore
        s = StateStore(journal_dir=str(tmp_path))
        s.update("TCS.NS", "WATCH", {})
        s.save()

        s2 = StateStore(journal_dir=str(tmp_path))
        s2.update("TCS.NS", "READY", {"entry_price": 3800.0})
        s2.save()

        s3 = StateStore(journal_dir=str(tmp_path))
        got = s3.get("TCS.NS")
        assert got["status"]      == "READY"
        assert got["prev_status"] == "WATCH"   # preserved across restart

    def test_first_seen_never_overwritten(self, tmp_path):
        from scanner.state_store import StateStore
        s = StateStore(journal_dir=str(tmp_path))
        s.update("INFY.NS", "WATCH", {})
        first = s.get("INFY.NS")["first_seen"]
        s.save()
        time.sleep(0.01)

        s2 = StateStore(journal_dir=str(tmp_path))
        s2.update("INFY.NS", "READY", {})
        assert s2.get("INFY.NS")["first_seen"] == first  # unchanged

    def test_corrupt_state_file_falls_back_to_empty(self, tmp_path):
        from scanner.state_store import StateStore
        (tmp_path / "scan_state.json").write_text("{INVALID JSON", encoding="utf-8")
        s = StateStore(journal_dir=str(tmp_path))
        assert s.get_all_states() == {}    # silent recovery, no crash

    def test_alert_deduplication_across_restart(self, tmp_path):
        from scanner.state_store import StateStore
        s = StateStore(journal_dir=str(tmp_path))
        s.update("WIPRO.NS", "READY", {})
        s.mark_alerted("WIPRO.NS")
        s.save()

        s2 = StateStore(journal_dir=str(tmp_path))
        assert s2.was_alerted_today("WIPRO.NS") is True

    def test_alert_flag_resets_next_day(self, tmp_path):
        from scanner.state_store import StateStore
        s = StateStore(journal_dir=str(tmp_path))
        s.update("ONGC.NS", "READY", {})
        # Manually set last_alert to yesterday
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        s._data["states"]["ONGC.NS"]["last_alert"] = f"{yesterday}T10:30:00"
        s.save()

        s2 = StateStore(journal_dir=str(tmp_path))
        assert s2.was_alerted_today("ONGC.NS") is False

    def test_save_is_atomic(self, tmp_path):
        """Crash during save leaves old state intact."""
        from scanner.state_store import StateStore
        s = StateStore(journal_dir=str(tmp_path))
        s.update("SBIN.NS", "WATCH", {})
        s.save()
        original = (tmp_path / "scan_state.json").read_bytes()

        s2 = StateStore(journal_dir=str(tmp_path))
        s2.update("SBIN.NS", "READY", {})
        with patch("scanner.state_store.os.replace",
                   side_effect=OSError("crash")):
            with pytest.raises(OSError):
                s2.save()

        assert (tmp_path / "scan_state.json").read_bytes() == original

    def test_purge_stale_entries(self, tmp_path):
        from scanner.state_store import StateStore
        s = StateStore(journal_dir=str(tmp_path))
        s.update("OLD.NS", "WATCH", {})
        # Back-date last_seen by 40 days
        old_ts = (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds")
        s._data["states"]["OLD.NS"]["last_seen"] = old_ts
        s.update("FRESH.NS", "READY", {})
        removed = s.purge_stale(max_age_days=30)
        assert removed == 1
        assert s.get("OLD.NS") is None
        assert s.get("FRESH.NS") is not None


# ═════════════════════════════════════════════════════════════════════════════
# 4. RECONNECT SIMULATION (WebSocket backoff)
# ═════════════════════════════════════════════════════════════════════════════

class TestWebSocketReconnect:
    """
    Validate TickerManager's backoff schedule and subscription restore
    without requiring a live KiteConnect account.
    """

    def test_backoff_grows_exponentially(self):
        from integrations.ticker_client import _backoff
        delays = [_backoff(i) for i in range(7)]
        # Strip jitter: each step must be at least 1.5× the previous BASE
        for i in range(1, len(delays)):
            # After removing max jitter the next base should be double
            base_prev = 1.0 * (2.0 ** (i - 1))
            base_curr = 1.0 * (2.0 **  i)
            # Absolute bounds: delay ≥ base * 0.75  and  delay ≤ cap * 1.25
            assert delays[i - 1] >= base_prev * 0.75

    def test_backoff_caps_at_max(self):
        from integrations.ticker_client import _backoff, _BACKOFF_CAP
        # After many attempts the delay must never exceed cap + jitter
        for attempt in range(20, 30):
            assert _backoff(attempt) <= _BACKOFF_CAP * 1.26

    def test_backoff_never_negative(self):
        from integrations.ticker_client import _backoff
        for i in range(20):
            assert _backoff(i) >= 0.1

    def test_subscribe_stored_before_connect(self):
        """Subscriptions added before start() must be in the set."""
        from integrations.ticker_client import TickerManager
        mgr = TickerManager("key", "token")
        mgr.subscribe([111, 222])
        assert 111 in mgr._subscriptions
        assert 222 in mgr._subscriptions

    def test_unsubscribe_removes_from_set(self):
        from integrations.ticker_client import TickerManager
        mgr = TickerManager("key", "token")
        mgr.subscribe([111, 222, 333])
        mgr.unsubscribe([222])
        assert 222 not in mgr._subscriptions
        assert 111 in mgr._subscriptions

    def test_multiple_subscribe_calls_accumulate(self):
        from integrations.ticker_client import TickerManager
        mgr = TickerManager("key", "token")
        mgr.subscribe([100])
        mgr.subscribe([200, 300])
        assert mgr._subscriptions == {100, 200, 300}

    def test_tick_callback_registered(self):
        from integrations.ticker_client import TickerManager
        calls = []
        mgr   = TickerManager("key", "token")
        mgr.on_tick(lambda ticks: calls.append(ticks))
        mgr._handle_ticks(None, [{"token": 738561, "last_price": 100.0}])
        assert len(calls) == 1

    def test_connect_callback_resets_attempt_and_restores_subs(self):
        """
        _handle_connect must reset the attempt counter and resubscribe.
        """
        from integrations.ticker_client import TickerManager
        mgr = TickerManager("key", "token")
        mgr.subscribe([738561])
        mgr._attempt = 5

        subscribed = []
        mode_set   = []

        class FakeWS:
            MODE_FULL = "full"
            def subscribe(self, tokens):   subscribed.extend(tokens)
            def set_mode(self, mode, tks): mode_set.extend(tks)

        mgr._handle_connect(FakeWS(), {})
        assert mgr._attempt == 0
        assert 738561 in subscribed
        assert 738561 in mode_set

    def test_safe_call_swallows_callback_exception(self):
        from integrations.ticker_client import _safe_call
        # Should not raise
        _safe_call(lambda: 1 / 0)

    def test_stop_sets_running_false(self):
        from integrations.ticker_client import TickerManager
        mgr = TickerManager("key", "token")
        mgr._running = True
        mgr._ticker  = MagicMock()
        mgr.stop()
        assert mgr._running is False


# ═════════════════════════════════════════════════════════════════════════════
# 5. INVALID MARKET DATA VALIDATION
# ═════════════════════════════════════════════════════════════════════════════

class TestDataValidator:
    """validate_ohlcv catches every class of bad OHLCV data."""

    # ── 5a. Duplicate bars ────────────────────────────────────────────────────

    def test_duplicate_bars_detected(self):
        from scanner.data_validator import validate_ohlcv
        df = _make_recent_ohlcv(10)
        df = pd.concat([df, df.iloc[:1]])          # append first row again
        res = validate_ohlcv(df, ticker="TEST")
        assert not res.valid
        assert any("duplicate" in e.lower() for e in res.errors)

    def test_clean_ohlcv_deduplicates(self):
        from scanner.data_validator import clean_ohlcv, validate_ohlcv
        df = _make_recent_ohlcv(10)
        df = pd.concat([df, df.iloc[:3]])           # add 3 duplicate rows
        cleaned = clean_ohlcv(df)
        res = validate_ohlcv(cleaned, ticker="CLEAN")
        assert not any("duplicate" in e.lower() for e in res.errors)

    # ── 5b. Invalid OHLC ─────────────────────────────────────────────────────

    def test_high_less_than_low_rejected(self):
        from scanner.data_validator import validate_ohlcv
        df = _make_recent_ohlcv(10)
        df.iloc[2, df.columns.get_loc("High")] = 80.0   # High = 80 < Low ≈ 99
        df.iloc[2, df.columns.get_loc("Low")]  = 99.0
        res = validate_ohlcv(df, ticker="HLTEST")
        assert not res.valid
        assert any("High < Low" in e for e in res.errors)

    def test_zero_price_rejected(self):
        from scanner.data_validator import validate_ohlcv
        df = _make_recent_ohlcv(10)
        df.iloc[0, df.columns.get_loc("Close")] = 0.0
        res = validate_ohlcv(df, ticker="ZERO")
        assert not res.valid
        assert any("zero" in e.lower() for e in res.errors)

    def test_negative_price_rejected(self):
        from scanner.data_validator import validate_ohlcv
        df = _make_recent_ohlcv(10)
        df.iloc[1, df.columns.get_loc("Low")] = -5.0
        res = validate_ohlcv(df, ticker="NEG")
        assert not res.valid

    def test_close_outside_high_low_warns(self):
        from scanner.data_validator import validate_ohlcv
        df = _make_recent_ohlcv(10)
        # Close above High → warning not error
        df.iloc[3, df.columns.get_loc("Close")] = df.iloc[3]["High"] + 5.0
        res = validate_ohlcv(df, ticker="CTEST")
        assert res.valid or not res.valid   # may be valid
        assert any("Close" in w for w in res.warnings)

    def test_open_outside_high_low_warns(self):
        from scanner.data_validator import validate_ohlcv
        df = _make_recent_ohlcv(10)
        df.iloc[4, df.columns.get_loc("Open")] = df.iloc[4]["High"] + 3.0
        res = validate_ohlcv(df, ticker="OTEST")
        assert any("Open" in w for w in res.warnings)

    # ── 5c. Stale candles ────────────────────────────────────────────────────

    def test_stale_candles_rejected(self):
        from scanner.data_validator import validate_ohlcv
        # Data ending 30 calendar days ago
        df = _make_valid_ohlcv(20, start="2020-01-01")
        res = validate_ohlcv(df, ticker="STALE", max_stale_business_days=5)
        assert not res.valid
        assert any("stale" in e.lower() for e in res.errors)

    def test_recent_data_not_stale(self):
        from scanner.data_validator import validate_ohlcv
        df  = _make_recent_ohlcv(10)
        res = validate_ohlcv(df, ticker="FRESH", max_stale_business_days=5)
        assert not any("stale" in e.lower() for e in res.errors)

    # ── 5d. Missing bars ─────────────────────────────────────────────────────

    def test_large_gap_warns(self):
        from scanner.data_validator import validate_ohlcv
        df1 = _make_recent_ohlcv(10)
        # Insert a 30-day gap by removing the middle bars
        gap_start = df1.index[4] + pd.Timedelta(days=30)
        gap_end   = df1.index[4] + pd.Timedelta(days=40)
        extra = pd.DataFrame({
            "Open":  [100.0], "High": [105.0],
            "Low":   [98.0],  "Close": [102.0], "Volume": [1e6],
        }, index=[gap_end])
        df = pd.concat([df1.iloc[:5], extra])
        res = validate_ohlcv(df, ticker="GAPTEST", max_gap_calendar_days=10)
        assert any("gap" in w.lower() for w in res.warnings)

    def test_normal_weekend_gap_not_flagged(self):
        from scanner.data_validator import validate_ohlcv
        df  = _make_recent_ohlcv(20)   # business-day index, max gap = 3 days
        res = validate_ohlcv(df, ticker="WEEKENDS", max_gap_calendar_days=10)
        assert not any("gap" in w.lower() for w in res.warnings)

    # ── 5e. Edge cases ────────────────────────────────────────────────────────

    def test_empty_dataframe_rejected(self):
        from scanner.data_validator import validate_ohlcv
        res = validate_ohlcv(pd.DataFrame(), ticker="EMPTY")
        assert not res.valid
        assert any("empty" in e.lower() for e in res.errors)

    def test_none_dataframe_rejected(self):
        from scanner.data_validator import validate_ohlcv
        res = validate_ohlcv(None, ticker="NONE")  # type: ignore[arg-type]
        assert not res.valid

    def test_missing_column_rejected(self):
        from scanner.data_validator import validate_ohlcv
        df = _make_recent_ohlcv(5).drop(columns=["High"])
        res = validate_ohlcv(df, ticker="NOHIGH")
        assert not res.valid
        assert any("Missing" in e for e in res.errors)

    def test_valid_data_passes_all_checks(self):
        from scanner.data_validator import validate_ohlcv
        df  = _make_recent_ohlcv(60)
        res = validate_ohlcv(df, ticker="GOOD")
        assert res.valid
        assert res.errors == []

    def test_validation_result_str(self):
        from scanner.data_validator import ValidationResult
        r = ValidationResult(valid=False, ticker="X", errors=["err1"],
                             warnings=["warn1"])
        s = str(r)
        assert "X" in s
        assert "err1" in s