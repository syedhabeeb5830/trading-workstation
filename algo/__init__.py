"""
algo/ — Intraday Algorithmic Trading Engine
=============================================
Standalone module for automated intraday execution.

Usage:
    python run.py --algo                   ← live paper mode (default)
    python run.py --algo --live            ← real orders (kill switch active)
    python run.py --algo --simulate        ← replay today's data through strategy
    python run.py --algo --status          ← show today's algo P&L and trades

Architecture:
    KiteTicker (WebSocket) → Candle Aggregator → Strategy → Risk Guard → Executor
"""
