"""
scanner/scanner.py  —  Market Intelligence Engine (Phase 6)
============================================================
Provides all pure computation functions used by scan_result.py.
No output/printing here — pure data functions only.

Also provides run_scanner() for diagnostic verbose mode.
"""

import logging
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime

from scanner.data_validator import validate_ohlcv, clean_ohlcv

_log = logging.getLogger(__name__)

try:
    from config.config import CONFIG
    from scanner.trade_engine import (
        compute_setup_score, compute_exec_score,
        compute_regime_score, build_trade_plan,
        compute_entry, compute_stops,
    )
    from scanner.watchlist import WATCHLIST
    from analytics.scan_logger import log_scan_results
except ModuleNotFoundError:
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from config.config import CONFIG
    from scanner.trade_engine import (
        compute_setup_score, compute_exec_score,
        compute_regime_score, build_trade_plan,
        compute_entry, compute_stops,
    )
    from scanner.watchlist import WATCHLIST
    from analytics.scan_logger import log_scan_results


# ── Market regime ──────────────────────────────────────────────────────────────

def get_market_regime(config: dict) -> dict:
    df = yf.download(config["market_index"], period=config["data_period"],
                     auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()

    if len(df) < config["regime_sma_slow"]:
        return {"regime": "NEUTRAL", "strength": 0.5, "index_df": df}

    df["SMA_fast"] = df["Close"].rolling(config["regime_sma_fast"]).mean()
    df["SMA_slow"] = df["Close"].rolling(config["regime_sma_slow"]).mean()

    close    = float(df["Close"].iloc[-1])
    sma_fast = float(df["SMA_fast"].iloc[-1])
    sma_slow = float(df["SMA_slow"].iloc[-1])

    high_20 = float(df["High"].tail(20).max())
    low_20  = float(df["Low"].tail(20).min())
    rng_20  = high_20 - low_20
    pos     = (close - low_20) / rng_20 if rng_20 > 0 else 0.5

    if close > sma_fast > sma_slow:
        regime, strength = "BULL",    min(1.0, 0.6 + 0.4 * pos)
    elif close < sma_fast < sma_slow:
        regime, strength = "BEAR",    max(0.0, 0.4 * pos)
    else:
        regime, strength = "NEUTRAL", 0.5 + 0.2 * (pos - 0.5)

    return {"regime": regime, "strength": round(strength, 3), "index_df": df}


# ── Data fetch ─────────────────────────────────────────────────────────────────

def fetch_stock_data(ticker: str, config: dict) -> pd.DataFrame | None:
    df = yf.download(ticker, period=config["data_period"],
                     auto_adjust=True, progress=False)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.dropna()
    if len(df) < config["min_bars"]:
        return None

    # Fix duplicates before validation
    df = clean_ohlcv(df)

    result = validate_ohlcv(df, ticker=ticker)
    for w in result.warnings:
        _log.warning(w)
    if not result.valid:
        _log.error("Dropping %s — data integrity failure: %s", ticker,
                   "; ".join(result.errors))
        return None

    return df


# ── Hard gates ─────────────────────────────────────────────────────────────────

def passes_liquidity_gate(df: pd.DataFrame, config: dict) -> bool:
    return (float(df["Volume"].tail(20).mean()) >= config["min_avg_volume"]
            and float(df["Close"].iloc[-1]) >= config["min_price"])


def compute_atr_series(df: pd.DataFrame, window: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(window=window).mean()


def passes_atr_gate(df: pd.DataFrame, config: dict) -> tuple[bool, float, float]:
    atr_s  = compute_atr_series(df)
    atr_v  = float(atr_s.iloc[-1])
    close  = float(df["Close"].iloc[-1])
    if close == 0: return False, 0.0, 0.0
    atr_pct = (atr_v / close) * 100
    return (config["min_atr_pct"] <= atr_pct <= config["max_atr_pct"],
            round(atr_pct, 2), round(atr_v, 2))


# ── Signal metrics ──────────────────────────────────────────────────────────────

def compute_trend_quality(df: pd.DataFrame) -> dict:
    d = df.copy()
    d["SMA20"]  = d["Close"].rolling(20).mean()
    d["SMA50"]  = d["Close"].rolling(50).mean()
    d["SMA200"] = d["Close"].rolling(200).mean()
    close = float(d["Close"].iloc[-1])
    s20   = float(d["SMA20"].iloc[-1])
    s50   = float(d["SMA50"].iloc[-1])
    s200  = d["SMA200"].iloc[-1]
    conds = {
        "above_sma20":       close > s20,
        "above_sma50":       close > s50,
        "sma20_above_sma50": s20 > s50,
        "above_sma200":      (not pd.isna(s200)) and close > float(s200),
    }
    sr = sum(conds.values())
    return {"conditions": conds, "score_raw": sr, "score_norm": round(sr / 4, 2)}


def compute_relative_strength(df: pd.DataFrame, index_df: pd.DataFrame,
                                lookback: int = 20) -> float:
    if len(df) < lookback or len(index_df) < lookback: return 0.0
    sr = (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-lookback]) - 1) * 100
    ir = (float(index_df["Close"].iloc[-1]) / float(index_df["Close"].iloc[-lookback]) - 1) * 100
    return round(sr - ir, 2)


def compute_consolidation(df: pd.DataFrame, config: dict) -> dict:
    n = config["consolidation_days"]
    r = df.tail(n)
    rh, rl, ra = float(r["High"].max()), float(r["Low"].min()), float(r["Close"].mean())
    if ra == 0: return {"range_pct": 999, "inside_bar_count": 0}
    rng = ((rh - rl) / ra) * 100
    ibc = sum(1 for i in range(1, len(r))
              if float(r["High"].iloc[i]) <= float(r["High"].iloc[i-1])
              and float(r["Low"].iloc[i]) >= float(r["Low"].iloc[i-1]))
    return {"range_pct": round(rng, 2), "inside_bar_count": ibc}


def compute_volume_behavior(df: pd.DataFrame, config: dict) -> dict:
    n   = config["consolidation_days"]
    avg = float(df["Volume"].tail(20).mean())
    rec = float(df["Volume"].tail(n).mean())
    if avg == 0: return {"volume_ratio": 1.0, "is_drying_up": False}
    ratio = rec / avg
    return {"volume_ratio": round(ratio, 2), "is_drying_up": ratio < 0.75}


def compute_breakout_proximity(df: pd.DataFrame, config: dict) -> dict:
    h20 = float(df["High"].tail(20).max())
    cur = float(df["Close"].iloc[-1])
    if h20 == 0: return {"distance_pct": 999, "near_breakout": False}
    dist = ((h20 - cur) / h20) * 100
    return {"distance_pct": round(dist, 2),
            "near_breakout": dist <= config["breakout_proximity_pct"]}