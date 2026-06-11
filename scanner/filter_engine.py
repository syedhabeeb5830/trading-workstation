"""
scanner/filter_engine.py  —  Dynamic Scan Profiles
===================================================
Named filter profiles that override config.py thresholds at runtime.
Never mutates CONFIG — always returns a deep copy.

Profiles:
  tight       High-conviction only (strictest, A+ setups)
  balanced    Default — matches config.py values (no overrides)
  aggressive  Relaxed for lean markets or light days
  discovery   Show everything — trader decides what to act on

Usage (CLI):
  python run.py --today                       balanced (default)
  python run.py --today --profile tight       strictest
  python run.py --today --profile aggressive  more candidates
  python run.py --today --profile discovery   full universe

Usage (code):
  from scanner.filter_engine import apply_profile
  config = apply_profile(CONFIG, "aggressive")
  regime, results = run_observable_scan(config, ...)
"""

import copy

# Dotted keys map to nested config: "tier_thresholds.PILOT" → config["tier_thresholds"]["PILOT"]
PROFILES: dict = {
    "tight": {
        "description":               "High-conviction only — A+ setups, strict gates",
        "min_rs":                     0.0,
        "max_consolidation_pct":      7.0,
        "min_trend_score":            2,
        "tier_thresholds.PILOT":      52.0,
        "tier_thresholds.TIER_2":     62.0,
        "tier_thresholds.TIER_1":     76.0,
        "min_rr_ratio":               2.5,
        "breakout_proximity_pct":     3.0,
        "max_atr_pct":                6.0,
        "max_extension_pct":          2.0,
        "ready_max_distance_pct":     1.0,
    },
    "balanced": {
        "description": "Default — uses config.py values unchanged",
    },
    "aggressive": {
        "description":               "Relaxed — more candidates in lean/neutral markets",
        "min_rs":                    -8.0,
        "max_consolidation_pct":     14.0,
        "min_trend_score":            1,
        "tier_thresholds.PILOT":     35.0,
        "tier_thresholds.TIER_2":    50.0,
        "tier_thresholds.TIER_1":    68.0,
        "min_rr_ratio":               1.8,
        "breakout_proximity_pct":     6.0,
        "max_atr_pct":               10.0,
        "max_extension_pct":          4.0,
        "ready_max_distance_pct":     2.0,
    },
    "discovery": {
        "description":               "Full universe — everything scored, trader decides",
        "min_rs":                   -15.0,
        "max_consolidation_pct":     25.0,
        "min_trend_score":            0,
        "tier_thresholds.PILOT":     20.0,
        "tier_thresholds.TIER_2":    40.0,
        "tier_thresholds.TIER_1":    60.0,
        "min_rr_ratio":               1.5,
        "breakout_proximity_pct":    10.0,
        "max_atr_pct":               15.0,
        "max_extension_pct":          8.0,
        "ready_max_distance_pct":     3.0,
    },
}

VALID_PROFILES = list(PROFILES.keys())


def apply_profile(config: dict, profile: str = "balanced") -> dict:
    """
    Returns a deep copy of config with the named profile's overrides applied.
    Raises ValueError for unknown profiles.
    """
    profile = profile.lower().strip()
    if profile not in PROFILES:
        raise ValueError(
            f"Unknown profile '{profile}'. "
            f"Valid: {', '.join(VALID_PROFILES)}"
        )

    c        = copy.deepcopy(config)
    overrides = {k: v for k, v in PROFILES[profile].items() if k != "description"}

    for key, val in overrides.items():
        if "." in key:
            parent, child = key.split(".", 1)
            if parent not in c:
                c[parent] = {}
            c[parent][child] = val
        else:
            c[key] = val

    return c


def profile_description(profile: str) -> str:
    return PROFILES.get(profile, {}).get("description", profile)


def profile_diff(config: dict, profile: str) -> list[str]:
    """
    Returns human-readable lines showing what changes this profile makes
    vs the current config. Useful for the cockpit header.
    """
    profile = profile.lower().strip()
    overrides = {k: v for k, v in PROFILES.get(profile, {}).items() if k != "description"}
    lines = []
    for key, new_val in overrides.items():
        if "." in key:
            parent, child = key.split(".", 1)
            old_val = config.get(parent, {}).get(child, "?")
        else:
            old_val = config.get(key, "?")
        if old_val != new_val:
            lines.append(f"{key}: {old_val} → {new_val}")
    return lines
