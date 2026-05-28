"""
algo/config.py — Algo Trading Configuration
=============================================
All tunable parameters for the intraday algo engine.
Separated from the swing config — different game, different rules.
"""

ALGO_CONFIG = {

    # ──────────────────────────────────────────────────────────
    # CAPITAL & RISK  (institutional-grade risk management)
    # ──────────────────────────────────────────────────────────
    "capital":                 100_000,    # ₹ dedicated algo capital
    "leverage":                5,          # MIS gives 5× on equity
    "max_risk_per_trade_pct":  0.5,        # 0.5% of capital per trade (₹500)
    "max_daily_loss_pct":      2.0,        # 2% daily loss → KILL SWITCH (₹2,000)
    "max_daily_profit_pct":    4.0,        # 4% daily profit → stop (greed guard)
    "max_trades_per_day":      6,          # hard cap — prevents over-trading
    "max_concurrent_positions": 2,         # max open at same time
    "max_loss_per_trade":      1000,       # ₹ absolute max loss per trade

    # ──────────────────────────────────────────────────────────
    # TIMING
    # ──────────────────────────────────────────────────────────
    "market_open":             "09:15",
    "orb_end":                 "09:30",    # opening range collection ends
    "trade_start":             "09:30",    # start taking trades after ORB
    "no_new_trades_after":     "14:30",    # no fresh entries after this
    "square_off_time":         "15:10",    # mandatory MIS close
    "market_close":            "15:30",

    # ──────────────────────────────────────────────────────────
    # ORB STRATEGY (Opening Range Breakout)
    # ──────────────────────────────────────────────────────────
    "orb_timeframe_minutes":   15,         # first 15-min candle = opening range
    "orb_buffer_pct":          0.05,       # % buffer above/below range for entry
    "orb_min_range_pct":       0.3,        # skip if range too narrow (noise)
    "orb_max_range_pct":       1.5,        # skip if range too wide (risk)
    "orb_target_multiple":     2.0,        # target = 2× range width
    "orb_max_trades_per_stock": 1,         # one ORB trade per stock per day
    "orb_confirmation_candles": 1,         # candles above/below range before entry

    # ──────────────────────────────────────────────────────────
    # INSTRUMENTS  (liquid FnO stocks for clean execution)
    # ──────────────────────────────────────────────────────────
    "instruments": [
        "RELIANCE",
        "HDFCBANK",
        "INFY",
        "TCS",
        "ICICIBANK",
        "SBIN",
        "BHARTIARTL",
        "ITC",
    ],
    "exchange": "NSE",

    # ──────────────────────────────────────────────────────────
    # CANDLE AGGREGATION
    # ──────────────────────────────────────────────────────────
    "candle_intervals": [5, 15],           # build 5-min and 15-min candles

    # ──────────────────────────────────────────────────────────
    # CIRCUIT BREAKERS  (Wall Street institutional safety)
    # ──────────────────────────────────────────────────────────
    "consecutive_loss_pause":  3,          # 3 losses in a row → pause 30 min
    "pause_duration_minutes":  30,
    "max_slippage_pct":        0.3,        # reject if fill > 0.3% from signal

    # ──────────────────────────────────────────────────────────
    # EXECUTION
    # ──────────────────────────────────────────────────────────
    "order_type":              "MARKET",   # MARKET for speed, LIMIT for control
    "product_type":            "MIS",      # intraday margin product
    "paper_mode":              True,       # DEFAULT SAFE — no real orders
}
