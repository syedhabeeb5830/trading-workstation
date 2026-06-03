"""
algo/screener.py — Automatic Instrument Screener
=================================================
Tests every stock in a 27-stock Nifty liquid candidate pool against the
filtered ORB config and ranks them by edge quality.  The top N are
automatically selected as the live trading universe.

Why this beats a hardcoded list
--------------------------------
• Sector rotation is real — IT leads one month, banks the next
• Individual stocks degrade (earnings gaps, FII flow reversals)
• Manual batch-testing 3+ runs by hand is wasteful and biased
• This screener runs every backtest and self-calibrates

Scoring methodology
-------------------
Each stock is tested in single-stock isolation (max_concurrent_positions=1)
so concurrent-trade position limits do not distort per-stock scores.

  score = net_per_trade × win_rate_factor × depth_factor

  net_per_trade  = average net P&L per filtered trade opportunity
  win_rate_factor = max(0.1, win_rate / 0.50)   — 50% WR = neutral
  depth_factor   = log(1 + n_trades / MIN_TRADES) — more signals = reliable

  Stocks with < MIN_TRADES filtered signals are excluded.

Candidate pool
--------------
27 Nifty 50 members selected for:
  • High average daily volume (good execution, tight spreads)
  • Mid-to-large cap (no circuit-breaker risk)
  • Clean intraday trend behaviour (ORB geometry works)
  • Valid Yahoo Finance NSE ticker (essential for data download)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Callable

_B   = "\033[1m"
_G   = "\033[92m"
_R   = "\033[91m"
_D   = "\033[2m"
_RST = "\033[0m"

# ─────────────────────────────────────────────────────────────────────────────
# CANDIDATE POOL  (27 Nifty 50 liquid stocks)
# ─────────────────────────────────────────────────────────────────────────────

CANDIDATE_POOL: list[str] = [
    # Technology — clean trend days, sector coherence
    "INFY", "TCS", "HCLTECH", "WIPRO", "TECHM",
    # Banks — high volume, sharp intraday displacement
    "HDFCBANK", "ICICIBANK", "KOTAKBANK", "AXISBANK", "SBIN",
    # Consumer staples — regime-consistent, slow but steady
    "HINDUNILVR", "ITC", "NESTLEIND",
    # Telecom / Diversified
    "RELIANCE", "BHARTIARTL",
    # Auto & Capital Goods — momentum-driven ORB
    "MARUTI", "TATAMOTORS", "EICHERMOT",
    # Financial services
    "BAJFINANCE",
    # Pharma — sector rotation plays
    "SUNPHARMA", "DRREDDY",
    # Infrastructure / Capital Goods
    "LT", "ADANIPORTS",
    # Consumer Discretionary
    "TITAN",
    # Cement / Conglomerate
    "ULTRACEMCO", "GRASIM",
    # Insurance
    "HDFCLIFE",
]

# Minimum filtered trades for a stock to be eligible for selection
MIN_TRADES: int = 4


# ─────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StockScore:
    symbol:          str
    n_trades:        int
    win_rate:        float    # 0.0–1.0
    net_pnl:         float    # total net P&L over the full screened period
    net_per_trade:   float    # net_pnl / n_trades
    gross_per_trade: float    # gross_pnl / n_trades (before fees)
    score:           float    # composite ranking score
    selected:        bool = False


# ─────────────────────────────────────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────────────────────────────────────

def _composite_score(npt: float, win_rate: float, n: int) -> float:
    """
    Composite score that rewards BOTH edge quality and signal frequency.

        score = npt × win_rate_factor × depth_factor

    win_rate_factor:  50% WR = 1.0 (neutral), 65% WR = 1.3 (bonus)
    depth_factor:     log scale — 8 trades = ~1.1×, 20 trades = ~1.7×
    Negative npt always produces a negative score → drops to bottom.
    """
    if n < MIN_TRADES:
        return float("-inf")
    win_factor   = max(0.1, win_rate / 0.5)
    depth_factor = math.log(1.0 + n / MIN_TRADES)
    return npt * win_factor * depth_factor


# ─────────────────────────────────────────────────────────────────────────────
# SCREENER
# ─────────────────────────────────────────────────────────────────────────────

def screen_candidates(
    replay_fn:    Callable,       # Backtester._replay_day
    all_data:     dict,           # {symbol: {date: list[Candle]}}
    nifty_data:   dict,           # {date: list[Candle]}
    trade_dates:  list[date],
    filtered_cfg: dict,           # FILTERED config (strictest gate)
    top_n:        int   = 8,
    min_score:    float = 0.0,    # only select stocks scoring above this
    min_floor:    int   = 4,      # always pick at least this many stocks
) -> tuple[list[str], list[StockScore]]:
    """
    Run the filtered ORB replay on every candidate in all_data,
    score each one, and return (selected_symbols, full_ranking).

    Selection logic
    ---------------
    1. Take all eligible stocks (score > min_score) up to top_n.
    2. If fewer than min_floor pass, backfill with the next-best stocks
       so we always trade at least min_floor instruments.

    Single-stock isolation: max_concurrent_positions=1 so position-limit
    interference doesn't distort per-stock metrics.
    """
    # Override concurrency so each stock gets a fair independent test
    solo_cfg = {
        **filtered_cfg,
        "max_concurrent_positions": 1,
        "max_trades_per_day":       2,
    }

    candidates = [s for s in CANDIDATE_POOL if s in all_data]
    scores: list[StockScore] = []

    for sym in candidates:
        sym_data = {sym: all_data[sym]}
        trades   = []

        for d in trade_dates:
            dr = replay_fn(
                d, sym_data, [sym], solo_cfg,
                nifty_data=nifty_data, use_filters=True,
            )
            trades.extend(dr.trades)

        n = len(trades)
        if n == 0:
            scores.append(StockScore(sym, 0, 0.0, 0.0, 0.0, 0.0, float("-inf")))
            continue

        net   = sum(t.net_pnl   for t in trades)
        gross = sum(t.gross_pnl for t in trades)
        wins  = sum(1 for t in trades if t.net_pnl > 0)
        wr    = wins / n
        npt   = net   / n
        gpt   = gross / n
        sc    = _composite_score(npt, wr, n)

        scores.append(StockScore(sym, n, wr, net, npt, gpt, sc))

    # Sort: highest score first; -inf scores (too few trades) go to bottom
    scores.sort(key=lambda x: (x.score != float("-inf"), x.score), reverse=True)

    # ── Selection: positive-score stocks first, then fill to min_floor ────
    eligible = [s for s in scores if s.score != float("-inf")]

    # Step 1 — all stocks beating the min_score threshold (up to top_n)
    above_cut = [s.symbol for s in eligible if s.score > min_score][:top_n]

    # Step 2 — backfill if below the minimum floor
    if len(above_cut) < min_floor:
        already = set(above_cut)
        backfill = [s.symbol for s in eligible if s.symbol not in already]
        above_cut += backfill[: max(0, min_floor - len(above_cut))]

    selected = above_cut
    for s in scores:
        s.selected = s.symbol in selected

    return selected, scores


# ─────────────────────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────────────────────

def print_screening_report(
    scores:    list[StockScore],
    top_n:     int   = 8,
    min_score: float = 0.0,
) -> None:
    """Print ranked screening leaderboard with a score-cutoff separator."""
    print(f"\n  {_B}{'═'*76}{_RST}")
    print(f"  {_B}  INSTRUMENT SCREENER  —  ORB Edge Ranking  (filtered config){_RST}")
    print(f"  {_D}  Single-stock isolation · composite score = net/trade × WR × depth{_RST}")
    print(f"  {_B}{'═'*76}{_RST}")
    print(
        f"  {'#':>3}  {'SYMBOL':12s}  {'TRD':>4}  {'WIN%':>6}  "
        f"{'NET/TRD':>10}  {'GROSS/TRD':>10}  {'SCORE':>8}  STATUS"
    )
    print(f"  {'─'*76}")

    cutoff_printed = False
    for rank, s in enumerate(scores, 1):
        # Print score-cutoff separator just before the first negative-score entry
        if (
            not cutoff_printed
            and s.score != float("-inf")
            and s.score <= min_score
        ):
            print(
                f"  {'─'*76}\n"
                f"  {_D}  ▼ score ≤ {min_score:+.0f}  (below cutoff — "
                f"backfill only if < {MIN_TRADES}-stock floor){_RST}\n"
                f"  {'─'*76}"
            )
            cutoff_printed = True

        if s.n_trades == 0:
            print(
                f"  {rank:>3}  {s.symbol:12s}  {'—':>4}  {'—':>6}  "
                f"{'—':>10}  {'—':>10}  {'—':>8}  {_D}no data{_RST}"
            )
            continue

        if s.score == float("-inf"):
            status = f"{_D}< {MIN_TRADES} signals{_RST}"
        elif s.selected:
            status = f"{_G}✓ SELECTED{_RST}"
        else:
            status = f"{_D}not selected{_RST}"

        wr_clr  = _G if s.win_rate      >= 0.50 else _R
        npt_clr = _G if s.net_per_trade >= 0    else _R
        sc_str  = f"{s.score:>+8.1f}" if s.score != float("-inf") else f"{'—':>8}"

        print(
            f"  {rank:>3}  {s.symbol:12s}  {s.n_trades:>4}  "
            f"{wr_clr}{s.win_rate * 100:>5.1f}%{_RST}  "
            f"{npt_clr}₹{s.net_per_trade:>+9,.0f}{_RST}  "
            f"₹{s.gross_per_trade:>+9,.0f}  "
            f"{sc_str}  {status}"
        )

    print(f"  {'─'*76}")
    selected = [s for s in scores if s.selected]
    above = sum(1 for s in selected if s.score > min_score)
    backfilled = len(selected) - above
    detail = f"  ({above} above cutoff"
    if backfilled:
        detail += f", {backfilled} backfilled to reach min_floor"
    detail += ")"
    print(
        f"\n  {_B}Selected universe  ({len(selected)} stocks):{_RST}  "
        f"{', '.join(s.symbol for s in selected)}"
        f"\n  {_D}{detail}{_RST}"
    )
    print(f"  {_B}{'═'*76}{_RST}\n")
