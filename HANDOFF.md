# Trading Workstation — Handoff (v1.0-RC1)

> **Single source of truth.** Read top-to-bottom before touching code or starting a new
> session. This documents the **dynamic swing screener** (Phases 1–10B) plus the RC1
> hardening freeze. A separate, older workstation also lives in this repo — see §3.

---

## 1. Project Purpose

A Python CLI that discovers, ranks, sizes, builds, and **manages** swing-trading positions
for NSE equities (Nifty 500 universe), evidence-first. It answers, in order:

1. What's strong? (sector + relative-strength leadership)
2. What's a high-quality setup? (composite ranking)
3. Should I trade it *today*? (actionability + entry context)
4. What kind of market is it? (multi-dimensional regime)
5. How much should I buy? (risk-based position sizing)
6. Which combination should I hold? (portfolio construction)
7. What do I do with what I already hold? (lifecycle: HOLD/ADD/REDUCE/EXIT/ROTATE)
8. **Does any of this actually have edge?** (walk-forward forward-return validation)

Platform: Windows 11, Python 3.14, single-process CLI. Data: yfinance (daily OHLCV) +
Zerodha KiteConnect (live quotes, holdings, orders — optional). No web UI / DB / daemon.

---

## 2. Core Philosophy

```
Hard rejects (garbage only)
   ↓
Soft scoring (graded, never binary pass/fail)
   ↓
Weighted ranking (leadership dominates)
   ↓
Actionability (tradable today?)
   ↓
Regime awareness (what market is this?)
   ↓
Portfolio construction (conviction × risk × regime)
   ↓
Lifecycle management (continuous, rule-based)
   ↓
Evidence (validate every decision against forward returns)
```

Rank, don't filter. Optimize from outcomes, not opinions. Never wire an unproven factor
into scoring.

---

## 3. Architecture — TWO systems in one repo

### 3a. NEW: Dynamic Screener (this handoff)
| Phase | Module | Role |
|---|---|---|
| 1 | `scanner/universe_builder.py`, `scanner/data_feed.py` | Nifty 500 universe (NSE→cache→emergency) + bulk cached OHLCV |
| 2 | `scanner/sector_engine.py` | Sector leadership (bottom-up median RS, weekly snapshots) |
| 3 | `scanner/relative_strength_engine.py` | RS vs Nifty + vs sector; buckets TOP_10/20/50 |
| 4 | `scanner/composite_engine.py` | Weighted composite + grade + drivers/warnings |
| 5 | `scanner/actionability_engine.py` + `scanner/entry_context.py` | Tradable-today + entry context + RR |
| 6 | `scanner/regime_engine.py` | Multi-dim regime (trend/breadth/vol/participation/leadership) |
| 7 | `scanner/adaptive_scoring.py` + `config/regime_profiles.yaml` | Regime-aware re-weighting |
| 8 | `analytics/edge_validation.py`, `analytics/rolling_validation.py` | Walk-forward forward-return edge validation |
| 9 | `portfolio/portfolio_engine.py` | Risk-based sizing + construction (sector/cluster/regime limits) |
| 10B | `portfolio/lifecycle_engine.py` + `config/theme_map.yaml` | HOLD/ADD/REDUCE/EXIT/ROTATE + theme cap + action labels |
| — | `screen/screen_runner.py` | Cockpit orchestrator (`build_screen()` runs the whole pipeline) |

`build_screen()` is the spine: universe → feed → sector → RS → composite → actionability →
regime → adaptive → leader_weeks, returning a `ScreenResult`.

### 3b. OLD: Original Workstation (separate, untouched by the rebuild)
`scanner/scanner.py`, `scan_result.py`, `daily_mode.py`, `trade_engine.py`,
`watchlist.py` (105 static), `filter_engine.py`, `morning.py`, `positions.py`,
`trade_placement.py`, `algo/*`. CLIs: `--today --positions --place --morning
--backtest --algo --orb-screen --kite-login --doctor --reconcile`.

**They are different systems** with different universes and different regime engines.
Shared utilities only: `integrations/zerodha.py`, `scanner/scanner.py::compute_atr_series`,
`scanner/data_validator.py`, parts of `config/config.py`.

> ⚠️ **The #1 source of confusion:** `--today` uses the OLD SMA regime
> (`get_market_regime`) which hard-blocks new longs in BEAR; `--screen` uses the NEW
> multi-dimensional `RegimeClassifier` which surfaces ranked opportunities and caps
> exposure via the portfolio engine. They can disagree. Both are "correct" for their
> system. This is documented, not a bug.

---

## 4. Proven Findings (the evidence — most important section)

From walk-forward rolling validation (103 weekly screens, 11,556 obs, all regimes):

| Finding | Status | Evidence (20D forward) |
|---|---|---|
| **Relative-Strength leadership** (MARKET_LEADER > LAGGARD) | ✅ **PROVEN** | +1.00%, **t=2.65** (also 10D t=2.55) |
| **Actionability** (ACTION_NOW > AVOID) | 🟡 **PROMISING, NOT PROVEN** | +3.12%, t=1.40 — big magnitude, n only ~26 |
| **Composite grade** (A-tier > D-tier) | ⚠️ **FRAGILE** | +0.46%, t=1.01 — did not replicate at high n |
| **Adaptive scoring** (vs composite) | ⚪ **NEUTRAL** | ≈ 0%, not significant — keep, don't over-weight |
| **Leader persistence** (8+ wk leaders) | ❌ **REFUTED** | persistent UNDER-perform; 60D −3.06%, t=−2.07 |

**Implications baked into the system:**
- Portfolio conviction is anchored on **RS first** (0.45), then actionability (0.35), then
  RR (0.20); composite grade is a ≤±5 secondary modifier only.
- Leader persistence is a **display-only EXHAUSTION warning**, never a positive score.
- Adaptive is kept but flagged experimental.

---

## 5. Production CLI

**New screener (validated path):**
```
python run.py --screen              # cockpit: regime, sectors, Top 20, Top 5 actionable
python run.py --screen --refresh    # force-refresh universe + OHLCV cache
python run.py --portfolio           # conviction-weighted, risk-based allocation
python run.py --review-portfolio    # lifecycle: HOLD/ADD/REDUCE/EXIT/ROTATE (Kite or last portfolio)
python run.py --validate-edge       # quick walk-forward edge analytics + EDGE_SCORE
python run.py --validate-rolling [--years 2 --every 1 --rsample N]   # full rolling significance (SLOW)
```
**Old workstation:** `--today --positions --place --morning --backtest --algo
--orb-screen --kite-login --doctor --reconcile` (separate system; see §3b).

**Audits (RC1):**
```
python verification/release_candidate.py    # full E2E system audit (15 checks)
python audit/consistency_report.py          # cross-subsystem consistency (5 checks)
python verification/phase{1..10b}.py        # per-phase acceptance (each 8/8)
python verification/rolling_validation.py   # rolling framework acceptance
```

---

## 6. Config Files

| File | Purpose | Loader / fallback |
|---|---|---|
| `config/config.py` | OLD-system account/filters/tiers (also some shared values) | Python dict |
| `config/regime_profiles.yaml` | Phase-7 adaptive weights per regime (each sums to 100) | PyYAML→minimal parser→built-in default |
| `config/theme_map.yaml` | Phase-10B macro-theme → NSE sectors (7 complexes) | PyYAML→minimal list parser→built-in default |

PyYAML is **not installed**; both YAML loaders have minimal hand-rolled parsers + emergency
Python defaults, so nothing breaks if YAML is missing/corrupt.

---

## 7. Journal & Report Structure (persisted history)

`Journal/` (weekly snapshots — keep, they accumulate validation value):
```
sector_history/<week>.json + sector_history.csv          # Phase 2
rs_history/<week>.json + rs_history.csv                   # Phase 3 (drives leader_weeks)
composite_history/<week>.json + composite_history.csv    # Phase 4
actionability_history/<week>.json + .csv                  # Phase 5 (entry/stop/target/RR/class)
regime_history/<week>.json + regime_history.csv          # Phase 6
adaptive_history/<week>.json + adaptive_history.csv      # Phase 7
edge_validation/edge_<date>.json, rolling_<date>.json    # Phase 8
portfolio_history/<week>.json + portfolio_history.csv    # Phase 9
lifecycle_history/<week>.json                            # Phase 10B
```
`reports/` (regenerated each run, git-ignored): `screen_<date>.{json,csv,md}`,
`portfolio_<date>.*`, `review_<date>.*`, `edge_report_<date>.*`, `rolling_report_<date>.*`,
plus RC1: `data_integrity_report.md`, `performance_report.md`, `consistency_report.md`,
`dead_code_report.md`.

`cache/` (git-ignored): `universe/` (7-day TTL), `ohlcv/<date>_<period>.csv` (per-day).

---

## 8. Known Limitations

1. **Two regime engines** (`--today` SMA gate vs `--screen` multi-dim) can disagree — by design.
2. **Theme cap (40%) is a lifecycle/ADD control, NOT a construction control.** Portfolio
   construction enforces sector(30%)+cluster(20%) only → a book can exceed 40% in one macro
   theme. Backlog.
3. **Actionability not yet statistically proven** — treat ACTION_NOW as an *execution aid*,
   not a validated alpha source. Needs more ACTION_NOW samples (n≈26).
4. **Adaptive scoring is experimental** — neutral in validation; not alpha-proven.
5. **Leader persistence intentionally excluded from scoring** (refuted; display-only).
6. **Pipeline recomputes per CLI** — `--screen`/`--portfolio`/`--review-portfolio` each call
   `build_screen()` independently (no shared in-session cache).
7. **Grade ranking is fragile** — rely on RS, not composite grade, as the dependable signal.
8. **PyYAML absent** — YAML configs parsed by minimal parsers (fine for current schemas).

---

## 9. Future Backlog (ideas only — DO NOT implement without explicit ask)

- Theme cap enforced in portfolio **construction** (not just lifecycle).
- Shared in-session pipeline cache (run `build_screen()` once per invocation).
- Prove/disprove actionability at higher n (5y weekly rolling, full 500 universe).
- Optional consolidation of the two systems (retire old `--today` path or unify regimes).
- Wire `--review-portfolio` to real Zerodha holdings end-to-end once the account holds positions.

---

## 10. Current Stable Release

- **Tag:** `v1.0-RC1`
- **State:** Phases 1–10B complete; every phase verification 8/8; RC1 audit 15/15;
  consistency 5/5; data integrity clean.
- **RC1 changes (non-logic hardening only):** UTF-8 stdout in `run.py` (fixes piped-output
  crash on Windows cp1252); unambiguous holdings source in `lifecycle_engine.resolve_holdings`.
- **Recommended git action:** commit on a branch, then
  `git tag -a v1.0-RC1 -m "Dynamic swing screener — RC1 freeze"`.
- **Freeze rule:** no new features / indicators / scoring formula / weight changes until a
  new session explicitly reopens development. Next focus = collect live weekly snapshots and
  let the rolling validator accumulate evidence.

_Update §4 (findings) and §10 (state) at the end of every session._
