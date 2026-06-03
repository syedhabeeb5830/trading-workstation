"""
algo/config.py — Algo Trading Configuration
=============================================
All tunable parameters for the ORB strategy engine.
Edit this file to change risk limits, instruments, or strategy thresholds.
"""

ALGO_CONFIG: dict = {
    # ── Account ─────────────────────────────────────────────────────────────
    "capital":               100_000,       # total account capital (INR)
    "leverage":              1,             # 1 = no leverage (CNC); 5 = intraday MIS

    # ── Universe ────────────────────────────────────────────────────────────
    "instruments": [
        "RELIANCE", "HDFCBANK", "INFY", "TCS",
        "ICICIBANK", "SBIN", "BHARTIARTL", "ITC",
    ],

    # ── ORB strategy ─────────────────────────────────────────────────────────
    "orb_timeframe_minutes":  15,           # opening range = first N-min candle
    "orb_buffer_pct":         0.05,         # breakout buffer above/below range
    "orb_min_range_pct":      0.3,          # skip if range too tight (choppy)
    "orb_max_range_pct":      1.5,          # skip if range too wide (news risk)
    "orb_target_multiple":    2.0,          # target = 2x range width
    "no_new_trades_after":    "11:00",      # ORB edge expires ~75 min after open; no new entries after this time

    # ── Candle timeframes ────────────────────────────────────────────────────
    "candle_intervals":       [15],         # used by MultiTimeframeCandleManager

    # ── Risk management ───────────────────────────────────────────────────────
    "max_loss_per_trade":     1_000,        # INR risk per trade (position sizing)
    "risk_pct_per_trade":     0.5,          # % of capital per trade (backup sizing)
    "max_daily_loss_pct":     2.0,          # kill switch: max daily loss %
    "max_daily_profit_pct":   4.0,          # greed guard: stop after 4% profit
    "max_trades_per_day":     6,            # max total trades per session
    "max_concurrent_positions": 2,          # max open trades at once
    "max_sector_positions":   1,            # max simultaneous positions in the same sector

    # ── Circuit breaker ───────────────────────────────────────────────────────
    "consecutive_loss_pause": 3,            # pause after N consecutive losses
    "pause_duration_minutes": 30,           # how long to pause (minutes)

    # ── Execution ────────────────────────────────────────────────────────────
    "square_off_time":        "15:10",      # mandatory EOD square-off time
    "paper_mode":             True,         # True = log only, no real orders
    "order_product":          "MIS",        # MIS=intraday, CNC=delivery

    # ── Exchange ─────────────────────────────────────────────────────────────
    "exchange":               "NSE",
}
