"""Tests for scanner/guards.py — the trade-allow decision surface."""
from __future__ import annotations
import pandas as pd
import pytest
from scanner.guards import (
    check_entry_drift, check_gap_up, check_setup_age, check_sector_cap,
    check_portfolio_heat, check_max_positions, check_daily_loss_circuit,
    GuardResult,
)


def test_entry_drift_allows_when_within_limit():
    plan = {"entry_price": 1000.0}
    r = check_entry_drift(plan, current_price=1010.0, max_drift_pct=1.5)
    assert r.allowed is True


def test_entry_drift_blocks_when_chasing():
    plan = {"entry_price": 1000.0}
    r = check_entry_drift(plan, current_price=1020.0, max_drift_pct=1.5)
    assert r.allowed is False
    assert r.code == "DRIFT"


def test_gap_up_blocks_large_open():
    plan = {"entry_price": 100.0}
    r = check_gap_up(plan, today_open=105.0, max_gap_pct=2.5)
    assert r.allowed is False


def test_setup_age_blocks_stale():
    r = check_setup_age("2020-01-01", max_age_days=10)
    assert r.allowed is False
    assert r.code == "STALE"


def test_setup_age_handles_missing():
    r = check_setup_age(None, max_age_days=10)
    assert r.allowed is True


def test_sector_cap_blocks_at_limit():
    plan = {"ticker": "HDFCBANK.NS"}
    open_pos = [{"ticker": "ICICIBANK.NS"}, {"ticker": "AXISBANK.NS"}]
    smap = {"HDFCBANK.NS": "BANKING", "ICICIBANK.NS": "BANKING",
            "AXISBANK.NS": "BANKING"}
    r = check_sector_cap(plan, open_pos, smap, max_per_sector=2)
    assert r.allowed is False
    assert r.code == "SECTOR"


def test_sector_cap_allows_other():
    plan = {"ticker": "UNKNOWN.NS"}
    r = check_sector_cap(plan, [], {}, max_per_sector=1)
    assert r.allowed is True


def test_portfolio_heat_blocks_when_exceeded():
    plan = {"max_loss_inr": 2000}
    open_pos = [{"risk_per_share": 50, "quantity": 30}]   # 1500 existing
    # 1500 + 2000 = 3500 → 3.5% > 3% cap → blocked
    r = check_portfolio_heat(plan, open_pos, account_capital=100_000,
                              max_heat_pct=0.03)
    assert r.allowed is False
    assert r.code == "HEAT"


def test_portfolio_heat_allows_within():
    plan = {"max_loss_inr": 500}
    r = check_portfolio_heat(plan, [], account_capital=100_000,
                              max_heat_pct=0.03)
    assert r.allowed is True


def test_max_positions_blocks_at_cap():
    open_pos = [{"ticker": "A"}, {"ticker": "B"}, {"ticker": "C"}]
    r = check_max_positions(open_pos, max_positions=3)
    assert r.allowed is False


def test_daily_loss_circuit_returns_allowed_when_no_journal(tmp_path):
    r = check_daily_loss_circuit(
        journal_dir=str(tmp_path),
        account_capital=100_000,
        breaker_pct=0.02,
    )
    assert r.allowed is True
