"""
config/config.py  —  Single Source of Truth
============================================
Edit ACCOUNT section before each session.
Everything else: tune only after reviewing analytics.

KEY CHANGES from old config:
  - Regime no longer MULTIPLIES score (destroys signal)
  - Regime now SETS TIER LIMITS (controls aggression)
  - Hard filters relaxed — soft scoring replaces binary rejection
  - Position deployment tiers replace binary ACTIONABLE/AVOID
  - Portfolio heat tracking added
  - Sector concentration limits added
"""

CONFIG = {

    # ──────────────────────────────────────────────────────────
    # ACCOUNT  — update these before each session
    # ──────────────────────────────────────────────────────────
    "account_capital":         100_000,   # ₹ total trading capital
    "max_risk_per_trade":      0.01,      # 1.0% max risk per trade (₹1,000 on 1L)
    "max_portfolio_heat":      0.03,      # 3.0% max total open risk across all trades
    "max_positions":           3,         # hard cap on simultaneous open trades
    "max_sector_positions":    1,         # max trades in same sector at once

    # ──────────────────────────────────────────────────────────
    # MARKET REGIME
    # ──────────────────────────────────────────────────────────
    "market_index":            "^NSEI",
    "regime_sma_fast":         20,
    "regime_sma_slow":         50,

    # ──────────────────────────────────────────────────────────
    # DATA
    # ──────────────────────────────────────────────────────────
    "data_period":             "6mo",
    "min_bars":                60,

    # ──────────────────────────────────────────────────────────
    # HARD GATES  (structural exclusions — not near-misses)
    # Loosened from old config: soft scoring handles the rest
    # ──────────────────────────────────────────────────────────
    "min_avg_volume":          300_000,   # 20d avg volume floor
    "min_price":               50,        # ₹ price floor
    "max_atr_pct":             8.0,       # raised from 6 — let scorer penalise
    "min_atr_pct":             0.8,       # lowered from 1.0
    "min_trend_score":         1,         # lowered from 2 — scorer handles quality

    # ──────────────────────────────────────────────────────────
    # SOFT FILTERS  (score penalty instead of hard reject)
    # These become score deductions, not exits
    # ──────────────────────────────────────────────────────────
    "min_rs":                  -5.0,      # relaxed from -3.0; scorer penalises weak RS
    "max_consolidation_pct":   10.0,      # relaxed from 8.0
    "consolidation_days":      5,

    # ──────────────────────────────────────────────────────────
    # ENTRY ENGINE
    # ──────────────────────────────────────────────────────────
    "breakout_proximity_pct":  4.0,       # widened from 3.0
    "entry_buffer_pct":        0.25,
    "max_extension_pct":       3.0,       # widened from 2.0


    # Gap-up rejection: if today's open > yesterday's high by this %, skip
    "max_gap_up_pct":          2.5,       # avoid chasing gap opens

    # Setup expiry: if setup is N days old without triggering, mark stale
    "setup_expiry_days":       10,

    # Entry drift: if live price has drifted above the planned entry by
    # this %, treat the setup as "chasing" and block new entries.
    "max_entry_drift_pct":     1.5,

    #FIX
    "breakout_proximity_pct":  4.0,       # widened from 3.0
    "entry_buffer_pct":        0.25,
    "max_extension_pct":       3.0,       # widened from 2.0
    
    # --- ADD THIS LINE TO FIX THE KEYERROR ---
    "ready_max_distance_pct":  1.5,       # Max % distance below entry price to qualify as READY status

    # ──────────────────────────────────────────────────────────
    # STOP ENGINE
    # ──────────────────────────────────────────────────────────
    "stop_atr_multiple":       1.5,
    "stop_swing_lookback":     10,
    "stop_selection":          "tighter",

    # ──────────────────────────────────────────────────────────
    # TARGET ENGINE
    # ──────────────────────────────────────────────────────────
    "target_r_levels":         [2.0, 3.0],
    "breakout_projection_multiple": 1.0,

    # ──────────────────────────────────────────────────────────
    # NEW: 3-DIMENSIONAL SCORING
    # setup_score  = chart quality (independent of market)
    # regime_score = market environment quality
    # exec_score   = tradability (RR, extension, volatility)
    #
    # CRITICAL DESIGN: regime adjusts TIER LIMITS, not setup score.
    # A great setup stays a great setup even in neutral market.
    # The regime just determines how aggressive you can be.
    # ──────────────────────────────────────────────────────────
    "setup_weights": {
        "trend_quality":       0.25,
        "relative_strength":   0.30,      # most predictive
        "compression_quality": 0.25,
        "volume_behavior":     0.20,
    },
    "exec_weights": {
        "breakout_proximity":  0.50,
        "volatility_quality":  0.50,
    },

    # ──────────────────────────────────────────────────────────
    # DEPLOYMENT TIERS  (replaces binary ACTIONABLE/AVOID)
    #
    # Tier    Setup  Regime  Size      Meaning
    # TIER_1  ≥72    BULL    100%      elite — full size
    # TIER_2  ≥55    BULL+   75%       strong setup
    # PILOT   ≥42    any     25-50%    decent but regime caution
    # WATCH   any    any     0%        not triggered yet
    # AVOID   <42    any     0%        poor expectancy
    # ──────────────────────────────────────────────────────────
    "tier_thresholds": {
        "TIER_1":   72,
        "TIER_2":   55,
        "PILOT":    42,
    },
    "tier_size_pct": {
        "TIER_1":   1.00,    # 100% of max_risk_per_trade
        "TIER_2":   0.75,    # 75%
        "PILOT":    0.35,    # 35%
        "WATCH":    0.00,
        "AVOID":    0.00,
    },

    # Regime overrides: NEUTRAL caps at TIER_2, BEAR caps at PILOT
    "regime_tier_cap": {
        "BULL":    "TIER_1",   # no cap
        "NEUTRAL": "TIER_2",   # can't do TIER_1 in neutral
        "BEAR":    "AVOID",    # no new longs in bear
    },

    # Min RR still enforced — this is fundamental math
    "min_rr_ratio":            2.0,

    # ──────────────────────────────────────────────────────────
    # ACTIVE POSITION LIFECYCLE  (used by --positions)
    #
    # trailing_atr_multiple   — once trade is >= +1R, suggest trail at
    #                           Close − N×ATR. Higher = looser trail.
    # time_stop_days          — if held >= N days with < +1R progress,
    #                           flag for time-stop exit.
    # failed_breakout_days    — within first N days after entry, if R
    #                           drops to failed_breakout_r, flag as
    #                           failed breakout (exit immediately).
    # failed_breakout_r       — R-multiple threshold for "failed".
    # ──────────────────────────────────────────────────────────
    "trailing_atr_multiple":   2.0,
    "time_stop_days":          20,
    "failed_breakout_days":    5,
    "failed_breakout_r":      -0.5,

    # ──────────────────────────────────────────────────────────
    # SECTOR MAP  (for concentration control)
    # ──────────────────────────────────────────────────────────
    "sector_map": {
        # Banking & Finance
        "HDFCBANK.NS": "BANKING", "ICICIBANK.NS": "BANKING",
        "SBIN.NS": "BANKING", "AXISBANK.NS": "BANKING",
        "KOTAKBANK.NS": "BANKING", "INDUSINDBK.NS": "BANKING",
        "BAJFINANCE.NS": "FINTECH", "BAJAJFINSV.NS": "FINTECH",
        # IT
        "INFY.NS": "IT", "TCS.NS": "IT", "WIPRO.NS": "IT",
        "TECHM.NS": "IT", "HCLTECH.NS": "IT",
        "PERSISTENT.NS": "IT", "COFORGE.NS": "IT",
        "TATAELXSI.NS": "IT", "KPITTECH.NS": "IT",
        # Capital Goods
        "LT.NS": "CAPGOODS", "SIEMENS.NS": "CAPGOODS",
        "ABB.NS": "CAPGOODS", "CUMMINSIND.NS": "CAPGOODS",
        "BEL.NS": "DEFENCE", "HAL.NS": "DEFENCE",
        "BHEL.NS": "CAPGOODS", "BDL.NS": "DEFENCE",
        # Auto
        "MARUTI.NS": "AUTO", "M&M.NS": "AUTO",
        "EICHERMOT.NS": "AUTO", "BAJAJ-AUTO.NS": "AUTO",
        # Pharma
        "SUNPHARMA.NS": "PHARMA", "CIPLA.NS": "PHARMA",
        "DIVISLAB.NS": "PHARMA", "DRREDDY.NS": "PHARMA",
        "TORNTPHARM.NS": "PHARMA",
        # FMCG
        "HINDUNILVR.NS": "FMCG", "ITC.NS": "FMCG",
        "NESTLEIND.NS": "FMCG", "TATACONSUM.NS": "FMCG",
        "BRITANNIA.NS": "FMCG",
        # Power & Energy
        "POWERGRID.NS": "POWER", "NTPC.NS": "POWER",
        "ADANIPOWER.NS": "POWER", "TATAPOWER.NS": "POWER",
        "CGPOWER.NS": "POWER", "SUZLON.NS": "POWER",
        "WAAREEENER.NS": "POWER",
        # PSU/Infra
        "RVNL.NS": "PSU_INFRA", "IRFC.NS": "PSU_INFRA",
        "IRCTC.NS": "PSU_INFRA", "RAILTEL.NS": "PSU_INFRA",
        "NBCC.NS": "PSU_INFRA", "PNCINFRA.NS": "PSU_INFRA",
        # Midcap
        "DIXON.NS": "ELECTRONICS", "POLYCAB.NS": "CABLES",
        "KEI.NS": "CABLES", "APLAPOLLO.NS": "METALS",
        "BSE.NS": "EXCHANGE", "CDSL.NS": "EXCHANGE",
        "CAMS.NS": "FINTECH",
        "DEEPAKNTR.NS": "CHEMICALS", "PIIND.NS": "CHEMICALS",
        "SRF.NS": "CHEMICALS", "NAVINFLUOR.NS": "CHEMICALS",
        "ASTRAL.NS": "PIPES", "HAVELLS.NS": "ELECTRONICS",
        "VOLTAS.NS": "ELECTRONICS",
        # Consumption
        "TRENT.NS": "RETAIL", "DMART.NS": "RETAIL",
        "NYKAA.NS": "RETAIL", "PAYTM.NS": "FINTECH",
        "JUBLFOOD.NS": "RETAIL", "ANGELONE.NS": "FINTECH",
        "MCX.NS": "EXCHANGE",
        # Metals
        "TATASTEEL.NS": "METALS", "JSWSTEEL.NS": "METALS",
        "HINDALCO.NS": "METALS", "JINDALSTEL.NS": "METALS",
        # Oil & Gas
        "RELIANCE.NS": "OIL_GAS", "ONGC.NS": "OIL_GAS",
        "IOC.NS": "OIL_GAS", "BPCL.NS": "OIL_GAS",
        "GAIL.NS": "OIL_GAS",
        # Telecom
        "BHARTIARTL.NS": "TELECOM", "IDEA.NS": "TELECOM",
        # Others
        "ULTRACEMCO.NS": "CEMENT",
        "SOLARINDS.NS": "DEFENCE", "KAYNES.NS": "ELECTRONICS",
        "CLEAN.NS": "CHEMICALS",
    },
}