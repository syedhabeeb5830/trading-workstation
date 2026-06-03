"""Tests for analytics/swing_backtest.py — fee math + report shape."""
from __future__ import annotations
import pytest
from analytics.swing_backtest import _fees, TickerStats, DEFAULTS


def test_buy_fees_positive_and_small():
    f = _fees("BUY", 100.0, 100)        # ₹10,000 trade
    assert 0 < f < 30                    # well under 0.3%


def test_sell_fees_include_stt():
    f_buy  = _fees("BUY",  100.0, 100)
    f_sell = _fees("SELL", 100.0, 100)
    # Sell side has STT (0.1%) → must exceed buy
    assert f_sell > f_buy


def test_defaults_are_conservative():
    assert DEFAULTS["risk_per_trade"] <= 1500
    assert DEFAULTS["target_r_multiple"] >= 1.5
    assert DEFAULTS["stop_atr_multiple"] >= 1.0
    assert DEFAULTS["slippage_pct"] >= 0.1


def test_tickerstats_qualification_defaults_to_false():
    s = TickerStats(ticker="X")
    assert s.qualified is False
    assert s.trades == 0
