"""Tests for scanner/capital.py — broker-derived capital safety."""
from __future__ import annotations
import pytest
from scanner.capital import get_effective_capital, CapitalInfo


def test_falls_back_to_config_when_no_kite(monkeypatch):
    monkeypatch.setattr(
        "integrations.zerodha.get_client_or_none",
        lambda: None,
    )
    info = get_effective_capital({"account_capital": 100_000})
    assert isinstance(info, CapitalInfo)
    assert info.capital == 100_000
    assert info.source == "config"


def test_returns_kite_values_when_available(monkeypatch):
    class _MockKite:
        def margins_equity(self):
            return {
                "available_cash": 47_000,
                "net":            90_000,
                "used":           10_000,
                "live_balance":   100_000,
            }
    monkeypatch.setattr(
        "integrations.zerodha.get_client_or_none",
        lambda: _MockKite(),
    )
    info = get_effective_capital({"account_capital": 50_000})
    assert info.source == "kite"
    assert info.capital == 100_000
    assert info.deployed == 10_000


def test_falls_back_when_kite_returns_zero(monkeypatch):
    class _ZeroKite:
        def margins_equity(self):
            return {"available_cash": 0, "net": 0, "used": 0, "live_balance": 0}
    monkeypatch.setattr(
        "integrations.zerodha.get_client_or_none",
        lambda: _ZeroKite(),
    )
    info = get_effective_capital({"account_capital": 75_000})
    assert info.source == "config"
    assert info.capital == 75_000
