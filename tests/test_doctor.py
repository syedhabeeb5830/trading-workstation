"""Tests for scanner/doctor.py — pre-flight check correctness."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
import pytest
from scanner.doctor import (
    _check_kite_session, _check_playbook_integrity, _check_config_sanity,
    _check_no_algo_live_enabled, _backup_playbook, CheckResult,
)


def test_kite_session_fails_when_missing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    r = _check_kite_session()
    assert r.passed is False
    assert r.critical is True


def test_kite_session_passes_with_today_session(monkeypatch, tmp_path):
    from datetime import date
    monkeypatch.chdir(tmp_path)
    Path(".zerodha_session.json").write_text(json.dumps({
        "date": date.today().isoformat(),
        "access_token": "abc",
        "user_id": "U1",
    }))
    r = _check_kite_session()
    assert r.passed is True


def test_kite_session_fails_when_stale(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    Path(".zerodha_session.json").write_text(json.dumps({
        "date": "2020-01-01", "access_token": "x", "user_id": "U1",
    }))
    r = _check_kite_session()
    assert r.passed is False
    assert "stale" in r.message.lower()


def test_playbook_integrity_passes_when_absent(tmp_path):
    r = _check_playbook_integrity(str(tmp_path))
    assert r.passed is True


def test_playbook_integrity_passes_on_clean_db(tmp_path):
    db = tmp_path / "playbook.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE trades (id INTEGER)")
    conn.commit()
    conn.close()
    r = _check_playbook_integrity(str(tmp_path))
    assert r.passed is True


def test_config_sanity_flags_aggressive_risk():
    cfg = {
        "max_risk_per_trade": 0.05,  # 5% - too aggressive
        "max_portfolio_heat": 0.03,
        "max_positions": 3,
        "daily_loss_breaker_pct": 0.02,
    }
    r = _check_config_sanity(cfg)
    assert r.passed is False


def test_config_sanity_passes_on_sound_config():
    cfg = {
        "max_risk_per_trade": 0.01,
        "max_portfolio_heat": 0.03,
        "max_positions": 3,
        "daily_loss_breaker_pct": 0.02,
    }
    r = _check_config_sanity(cfg)
    assert r.passed is True


def test_algo_live_check_detects_freeze():
    r = _check_no_algo_live_enabled({})
    # Either passes (frozen) or skips (module missing) — never CRITICAL fail
    assert not (r.critical and not r.passed)


def test_backup_playbook_no_op_when_missing(tmp_path):
    _backup_playbook(str(tmp_path))   # should not raise
    assert not (tmp_path / "playbook.db.bak").exists()


def test_backup_playbook_creates_copy(tmp_path):
    db = tmp_path / "playbook.db"
    db.write_bytes(b"sqlite-format-3")
    _backup_playbook(str(tmp_path))
    assert (tmp_path / "playbook.db.bak").exists()
