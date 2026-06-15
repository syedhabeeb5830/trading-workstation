# Trading Workstation — Handoff (v1.0-RC1)

> **Single source of truth.** Read top-to-bottom before touching code or starting a new
> session. This documents the **dynamic swing screener** (Phases 1–10B) plus the RC1
> hardening freeze. A separate, older workstation also lives in this repo — see §3.

---

# SYSTEM VALIDATION STATUS (June 2026)

> **Authoritative current-state block.** A brand-new chat can read THIS section alone to
> understand the entire project state — research is complete **through the Robustness Audit
> (Phase 4 / F), Trust Validation Audit (Phase G), OOS Failure Forensics (Phase H / Phase 5), the
> System Repair Lab (Phase I / Phase 6 — a DESIGN; nothing implemented), the ATR Alpha-vs-Beta
> Audit (Phase J / Phase 6A — verdict E) Mixed: ATR is a volatility/risk-premium that survives OOS)
> and the Regime-Gated Leader Model (Phase K / Repair-Lab E4 — verdict B) Small improvement)**.
> No production code/scoring has been changed by any of these (the E4 gate was measured on the
> cached replay, NOT wired into the pipeline — the "no scoring/regime/ranking math changed"
> invariant still holds). The ATR gate (E5) is resolved → Branch 2; **E4 is now executed**: on the
> frozen 2025–26 holdout the BULL gate flips OOS leader-excess −0.07%→+0.49% and roughly halves max
> drawdown (−20.0%→−8.9%), but the gated excess CI **[−0.18, 1.08] still includes 0** (t=1.88) so the
> pre-registered E4 success criterion FAILS — the gate is a proven *risk overlay*, not yet a
> deployable edge. Next: de-dilution (E1/E2) + a transaction-cost model, then re-score Trust. The
> numbered sections below (`## 1. Project Purpose` … `## 10`) are the original RC1 handoff detail.

## 1. Original Goal

A Python CLI swing-trading workstation for NSE equities (Nifty 500 universe), built
**evidence-first**. The validated path is a ranking pipeline:

    universe → sector leadership → relative strength → composite → actionability → regime → portfolio → lifecycle

Objective: discover **high-quality swing-trade candidates** (leaders among leaders with good
entry geometry), size them by risk, manage their lifecycle — and **prove every factor against
forward returns** before trusting it. Governing rule (frozen RC1): **any scoring / threshold /
factor / regime / ranking change requires validation FIRST.** Rank, don't filter; optimise from
outcomes, not opinions.

## 2. Major Completed Phases (chronological)

### Phase A — Retirement Audit (legacy `--today` discovery engine)
- **Investigated:** whether the old 101-name static watchlist + setup-score engine adds any
  outcome value over the validated Nifty-500 `--screen`.
- **Key findings:** `--today` covered **0% of the screen's Top-10 leaders** and **12.5% of all
  MARKET_LEADERs**; its READY/WATCH picks underperformed the equal-weight universe in-window; the
  one live contradiction tested (KPITTECH: today WATCH vs screen LAGGARD/AVOID) resolved **−6.6%**
  in the screen's favour; the only proven edge (RS) lives entirely in `--screen`.
- **Outcome:** verdict **C — DEMOTE** (not delete). Record: `RETIREMENT_AUDIT.md`.

### Phase B — `--today` Demotion
- **Why deprecated:** no demonstrated incremental alpha; structurally blind to validated leadership.
- **What changed (display/doc ONLY):** deprecation banner on the `--today` cockpit
  (`daily_mode._print_deprecation_banner`), CLI help text, HANDOFF notes.
- **What did NOT change:** no scoring/regime/ranking/logic; `--today` still runs; all execution
  tooling (`--place`, `--positions`, `--reconcile`, …) retained until ported.

### Phase C — Data Resilience Audit + Fix
- **Root cause:** the RS eligibility gate hard-required the Nifty benchmark; a sector-relative
  fallback already existed but was discarded.
- **The `^NSEI` failure chain:** `^NSEI` download fails → `fetch_nifty()=None` → every stock fails
  the eligibility gate → all forced to **NEUTRAL / score 0** → leadership **95→0** → regime
  **NEUTRAL→WEAK_BEAR** → composite **−15** (leaders) → ACTION_NOW **4→0** — and the cockpit still
  showed "data=100%" (silent).
- **Why missing benchmark created false bearish outcomes:** leadership/regime are computed from RS,
  which the missing benchmark had zeroed — i.e. *missing data was being read as bearish data.*
- **Summary of fixes (reliability-only):**
  - **RS fallback honoured** — eligibility no longer requires the benchmark; snapshot carries
    `rs_mode` (FULL | SECTOR_FALLBACK) + `benchmark_status` (OK | MISSING).
  - **Benchmark cache** — `cache/benchmarks/` write-through; live → cache → missing.
  - **Data-quality reporting** — cockpit DATA QUALITY verdict (HEALTHY / DEGRADED / FAILED) + degraded banner.
  - **Fail-loud policy** — total `^NSEI` loss (live AND cache) aborts the screen
    (`BenchmarkUnavailableError`); `allow_degraded=True` is a research-only escape.
- **Invariant:** **benchmark present → screen identical to pre-fix behaviour** (verified live:
  regime NEUTRAL, ACTION_NOW=4 — unchanged).
- **13/13 validation passed** (`verification/data_resilience.py`). **No scoring changes.**
  Record: `DATA_RESILIENCE_AUDIT.md`.

### Phase D — 5-Year Validation (measurement only)
- **Sample size (exact):** **218 weekly screens · 94,151 observations · 2022-04-01 → 2026-05-29.**
- **Replay methodology (exact):** production Phase 2–7 engines replayed **point-in-time**
  (`df.index ≤ as_of`, no look-ahead), **weekly** cadence (the cadence the system runs/persists);
  forward returns at **5 / 10 / 20 / 60 / 120 trading days** + max favourable/adverse excursion;
  primary horizon 20D. Module `analytics/action_now_validation.py`; report
  `reports/validation/action_now_5y.md`.
- **Classification findings (20D):**
  - **ACTION_NOW:** +2.72% (n=**180**, win 57.8%) vs AVOID +1.83% → diff +0.89%, **t=−0.96 — NOT significant.**
  - **WATCHLIST:** +2.16% (n=11,900) vs universe +1.96% → **t=1.8 — not significant.**
  - **EXTENDED:** +3.85% (n=4,088) vs universe → **t=8.41 — SIGNIFICANT (OUTPERFORMS, opposite of the "don't chase" intent).**
  - **AVOID:** +1.83% (n=76,525) — **did NOT protect capital** (still positive; only ~0.9% below ACTION_NOW, not significant).
- **Statistically significant findings (|t| ≥ 1.96):** EXTENDED > universe (t=8.41);
  MARKET_LEADER > SECTOR_LEADER +0.93% (t=5.01); strong-vs-weak regimes **INVERTED** (t=−10.34).
- **Non-significant findings:** ACTION_NOW > AVOID (t=−0.96); ACTION_NOW > universe (t=0.81);
  WATCHLIST > universe (t=1.8); A+ > A +1.37% (t=1.12, n=135).
- **Composite/grade:** deciles **monotonic** (A+ +4.45% → D +1.54%) but continuous corr ≈0.04 (edge in the extremes only).
- **Validation was measurement-only: no thresholds changed, no factors changed, no ranking logic changed.**

### Phase E — Edge Attribution (measurement only)
Module `analytics/edge_attribution.py`; report `reports/validation/edge_attribution_audit.md`
(same 218-screen / 94,151-obs dataset, with per-stock component scores captured).

**Factor ranking — independent edge (top−bottom quintile, 20D spread):**

| Factor | Spread | Stable (yrs +) | Note |
|---|--:|--:|---|
| ATR | **+2.44%** | 100% | strongest; **likely a volatility/beta exposure** (flips −0.19 in WEAK_BEAR) |
| Sector | **+1.68%** | 80% | monotonic, additive with RS |
| Composite | +1.25% | 60% | the blend — scores **below** ATR-alone & Sector-alone |
| Actionability | +1.01% | 60% | inherits composite |
| Relative Strength | +0.51% | 80% | weak as a gradient; real only at the top bucket |
| Trend | +0.37% | 40% | redundant with RS |
| Freshness | −0.51% | — | noise (~0 yearly corr) |
| Breakout | **−1.06%** | **0%** | negative in every year |
| Liquidity | **−1.76%** | 20% | most negative |

- **Strongest predictors:** ATR (caveat: beta), Sector.
- **Weakest / noise predictors:** Trend, Freshness.
- **Redundant predictors:** Trend ≈ RS; Actionability ≈ Composite.
- **Dilutive predictors (net-negative):** Breakout (−1.06%, 20% composite weight) and Liquidity
  (−1.76%, 10% weight) — both DILUTE the RS signal (RS+Liquidity drops RS from +0.51% to −0.40%).
- **Composite blend (+1.25%) scores below its own best single inputs** (ATR/Sector) — diluted by
  its weighting (Breakout 20% / Liquidity 10% vs ATR 5%).
- Recorded exactly as measured — **no interpretation beyond evidence, no recommendations, no modifications.**

### Phase F — Robustness Audit (measurement only)
Module `analytics/robustness_audit.py`; report `reports/validation/robustness_audit.md`
(same 218-screen / 94,637-obs replay, dataset cached to `reports/validation/robustness_dataset.csv`).
Six durability lenses on the proven RS-leadership / composite-extreme edge (ACTION_NOW reported
but n=180 is too small to classify on its own). Headline numbers reproduce Phases D/E exactly
(universe 20D 1.96%, MARKET_LEADER 3.03% n=9379, ACTION_NOW 2.72% n=180).

- **Time stability — UNSTABLE / CONCENTRATED.** Leader 20D excess by period (P1→P5, 2022→2026):
  **+0.46 / +3.45 / +1.50 / −0.28 / +0.53%**. Positive in 4/5 periods but the single strongest
  period (P2 = Feb–Dec 2023 bull) supplies **0.58 of all positive excess.** It is **NOT** primarily
  the 2022 STRONG_BEAR bounce (P1 excess only +0.46%) — it is a **2023-bull-concentrated** edge.
- **Regime stability — BULL-ONLY; REVERSES IN BEAR.** Leader−Laggard 20D: BULL **+2.32% (t=13.21,
  holds)**; NEUTRAL +0.48% (t=0.95, weak); **BEAR −1.24% (t=−3.25, REVERSES significantly)** —
  leaders underperform laggards in bear (mean-reversion; consistent with the inverted strong-vs-weak
  regime finding and the refuted leader-persistence).
- **Sector dependency — BROAD.** Removing the strongest sector (**Capital Goods**, degrades edge
  0.37%) still leaves leader-excess **+0.7% (t=4.38)** — edge survives. No single sector carries it.
- **Stock dependency — BROAD (leaders) / CONCENTRATED (ACTION_NOW).** MARKET_LEADER has 402 distinct
  names; the **top-10 supply only 0.26 of positive excess** and removing them leaves +0.5% (t=3.33).
  By contrast ACTION_NOW (112 names) top-10 supply **0.62** — removing them flips it negative.
- **Factor dependency (equal-weight leave-one-out, baseline spread 1.48%).** **Essential: Sector
  (Δ+0.56), ATR (Δ+0.81)** — ATR with the volatility/beta caveat (inverts in WEAK_BEAR). **RS is
  redundant in the equal-weight blend** (Δ+0.15 — it rides Sector/Trend); **Liquidity dilutes**
  (Δ−0.54, removing it helps); Breakout ~neutral (Δ−0.30).
- **Failure clusters.** Worst-decile outcomes are **high-drawdown** (leader MAE −22.1% vs cohort
  −8.65%; ACTION_NOW −19.3% vs −8.52%) but **do not cluster on a distinct factor profile** (worst
  ≈ cohort on every component) — i.e. losers are not separable by the existing scores. ACTION_NOW
  failures skew to **Capital Goods (9/18)**; leader failures track the largest leader sectors
  (Capital Goods, Financials).
- **Final classification: B) Promising but fragile** (3/5 robustness checks: broad-across-stocks ✓,
  survives-strongest-sector ✓, positive-in-≥4/5-periods ✓; stable-in-time ✗, regime-consistent ✗).
- **Measurement only — no scoring / threshold / regime / ranking / factor changed; no recommendations.**

### Phase G — Trust Validation Audit (measurement only)
Module `analytics/trust_audit.py`; report `reports/validation/trust_audit.md`. Built on the same
cached 94,637-obs dataset; answers ONE question — *would deploying capital tomorrow be justified?*
Seven lenses (OOS walk-forward, decision-quality, month-by-month stability, bootstrap CIs,
deployment sim, failure modes, a 5-dimension Trust Score). Headline numbers again reproduce
Phases D/E exactly.

- **Out-of-sample (the decisive test) — the edge does NOT hold out-of-sample.** True chronological
  holdouts: in **2025** the leader edge **FAILS** (MARKET_LEADER eval-excess **−0.14%**, ACTION_NOW
  **−2.88%**, both ≤0); in **2026** (partial) MARKET_LEADER only **+0.11%** (weakens), ACTION_NOW
  +0.54% (n=18). The whole-sample edge was carried by 2022–2024 (esp. 2023) and does not appear in
  the year it never "saw".
- **Decision quality — the system DOES sort better than no-skill.** Top-10-by-composite expectancy
  **3.21%** 20D vs random-10 **1.62%** and equal-weight universe **1.96%**; ACTION_NOW 2.72%. So the
  ranking adds real per-trade separation in-sample — but see OOS.
- **Confidence intervals (date-clustered bootstrap, B=2000).** **MARKET_LEADER − universe +1.06%
  CI [0.68, 1.45] excludes 0 (REAL)**; Top-10 − universe +1.24% CI [0.51, 1.96] (REAL); **ACTION_NOW
  − AVOID +0.90% CI [−0.99, 2.91] INCLUDES 0 (could be noise)**; ACTION_NOW − universe also includes 0.
- **Stability — survivable but with long droughts.** Top-10 basket: 34/50 months positive, monthly
  stdev 5.5%; **longest losing streak 4 months, longest underperformance streak 5 months**
  (ending 2025-06); worst 3-month stretch −11.9% (2024-12→2025-02).
- **Deployment sim (weekly rebalance, equal-weight, no leverage, GROSS of costs, in-sample).**
  Top-10 CAGR 35.6%, vol 27%, **max DD −35.6%, and the basket has been BELOW its high-water mark for
  the trailing ~100 weeks** (peak set ≈ early-2024, after the 2023 surge; underwater since) —
  corroborates the time-concentration. ACTION_NOW(+cash) basket barely grows (1.13×, Sharpe 0.1).
- **Failure modes.** Failure (neg 20D) rate is **flat across regimes within a cohort** (~0.43) —
  losses are partly anticipatable *by regime* (concentrate where the leader edge weakens/reverses)
  but **not anticipatable within a regime**: the scores that pick names do not flag which will fail.
- **Trust Score: 53/100 → "Research Only"** (Data Integrity 98 · Statistical Evidence 70 ·
  Robustness 55 · Out-of-Sample 33 · Deployment Readiness 27).
- **Final verdict — would I trust this with real money? PARTIALLY.** The RS-leadership signal is real
  in-sample and broad-based, but it did **not** survive the 2025 holdout, the system is
  bull-regime-dependent and time-concentrated, and ACTION_NOW is indistinguishable from noise. Partial
  trust attaches to the **leader signal as a research input**, NOT to deploying the system as-is.
- **Measurement only — nothing tuned, optimised, redesigned or changed; no recommendations.**

### Phase H — Out-of-Sample Failure Forensics (Phase 5, measurement only)
Module `analytics/oos_forensics.py`; report `reports/validation/oos_failure_forensics.md`. A
post-mortem of WHY the edge failed the 2025 holdout (nine lenses on the same cached dataset). It
explains the failure; it does NOT fix it.

- **Verdict: C) Edge was period-specific.** ΔLeader-excess (IS 2022–24 → OOS 2025–26) = **−1.79%**,
  and it is **not monotone decay** — leader excess by year was **+0.76 / +3.28 / +1.06 / −0.14 /
  +0.11%** (spiked in 2023, ~0 thereafter). The edge was real and broad in-sample but contingent on
  the 2022–2024 (esp. 2023-bull) conditions.
- **Two forces, both present (failure decomposition, §7):** **~59% regime/sector composition shift**
  (regime −0.46% + sector −0.59% — the market genuinely changed) and **~41% within-group selection
  decay** (−0.74% — leaders stopped out-returning their own peers). Neither alone explains it.
- **The market did shift:** BULL regime share **0.87→0.56**, BEAR **0.09→0.29**, mean trend score
  **58→40**, breadth 0.57→0.51 (2022–24 vs 2025–26).
- **But the scoring also stopped predicting:** leaders' **core RS/sector/trend profile barely moved
  (5.7 pts)** while their excess return collapsed **+1.72%→−0.07%** — the same kind of names were
  flagged; they stopped winning. **Per-year factor IC: RS and Trend INVERTED out-of-sample** (RS
  IS +0.042 → OOS −0.001; Trend +0.046 → −0.035); ATR was the only factor that held/strengthened.
- **ACTION_NOW failure** (IS n=113 ret +4.94% → OOS n=67 ret −1.01%): composite/RS/actionability
  scores essentially unchanged (no score inflation); causes = score-stopped-predicting + regime
  mismatch (BULL share of AN 0.93→0.61) on top of an already-too-small, noise-level sample.
- **Counterfactual (deploy one year only):** a trader gains confidence in **3/5** years (2022–2024,
  made money AND beat market) and **loses** it in 2025 and 2026 (negative year CAGR).
- **Not overfit (rules out D):** Phases F/G already showed the edge broad across stocks/sectors with
  a bootstrap CI excluding zero — it was genuine, just period/regime-specific.
- **Measurement only — no weights/thresholds/classifications/rankings/factors/regime/portfolio logic
  changed; no optimisation, redesign or recommendation.**

### Phase I — System Repair Lab (Phase 6, DESIGN ONLY)
Report `reports/validation/system_repair_lab.md`. **First phase where logic changes are permitted to
be *designed* — but it is design only: NO code, NO implementation, NO tuning was done, and NO logic
was changed.** It specifies the smallest change-set that *could* move the system from "Research Only"
toward "Potentially Deployable," as pre-registered hypotheses to be validated out-of-sample later.

- **Component inventory (§1):** KEEP **Sector**, **RS** (as a regime-gated *top-bucket* classifier,
  not a continuous score), **Regime** (repurposed as a *gate*, not a direction signal); **INVESTIGATE
  ATR** (alpha vs beta — pivotal); REDUCE **Grade** & the **Classification** layer; **REMOVE Trend,
  Breakout, Liquidity, Freshness** from scoring (net-negative / noise / redundant; Liquidity retained
  only as a tradability floor).
- **Minimum viable model (§3):** if only 3 factors survive → **Sector · RS · ATR.** Caveat: if the
  ATR beta test fails, the genuine skill core collapses to **Sector + RS**.
- **Model hierarchy (§4):** A current · B de-diluted (drop Breakout/Liquidity/Trend/Freshness) ·
  C Sector+ATR-dominated · D regime-aware (apply leader edge in BULL, stand aside in NEUTRAL/BEAR).
- **Experiments (§5–6):** 7 one-change A/B experiments, each with a pre-registered OOS success
  criterion. HIGH-ROI = **E5 ATR-beta test, E4 regime-gate, E1 drop-Breakout**; MED = E2 drop-Liquidity,
  E6 equal-weight survivors; LOW = E3 drop-Trend/Freshness, E7 demote-ACTION_NOW.
- **Deployment blockers (§7, ranked):** 1) no OOS edge · 2) BEAR reversal with no gate · 3) dilutive
  factors in scoring · 4) ATR alpha/beta unresolved · 5) no transaction-cost model · 6) deep
  unrecovered drawdown · 7) ACTION_NOW is noise · 8) failures not pre-identifiable.
- **Roadmap (§8):** critical path = **E5 (ATR alpha/beta) → E4 (regime-gate) → re-test the frozen
  2025–26 holdout**; de-dilution (E1/E2/E3) and E6/E7 are scoring hygiene. Decision gate: deployable
  candidate ONLY IF OOS edge CI excludes 0 AND regime-gated DD acceptable AND not erased by costs;
  else conclude "no deployable edge survives."
- **Nothing implemented. The "no scoring/regime/ranking math changed anywhere" invariant STILL HOLDS.**

### Phase J — ATR Alpha-vs-Beta Audit (Phase 6A / Repair-Lab E5, measurement only)
Module `analytics/atr_alpha_beta.py`; report `reports/validation/atr_alpha_beta_audit.md`. The
single highest-information experiment: is ATR (the only OOS-durable factor) genuine alpha or
exposure? Beta estimated from daily Close (read **directly** from `cache/ohlcv/*_5y.csv` +
`cache/benchmarks/NSEI_5y.csv` — no `get_ohlcv`/`fetch_nifty`, fully offline; per-ticker beta cached
to `reports/validation/atr_beta_by_ticker.csv`, reuse with `--reuse-beta`). Forward returns
beta-neutralised; ATR re-measured before/after, within sector, within regime, OOS.

- **Verdict: E) Mixed — ATR is a volatility / risk-premium factor that SURVIVES out-of-sample, but
  is NOT proven stock-selection skill and is NOT a simple market-beta proxy.**
- **Not pure market-beta:** market beta (Nifty50) explains only **~20%** of ATR's raw 20D spread;
  the β-neutral residual keeps **~80%** (1.96% of 2.44%); ATR↔β Pearson only **0.216** (the
  hump-shaped score discards the highest-β/highest-vol names); ATR adds independent edge beyond
  **both** RS (0.51%→2.33%) and Sector (1.68%→2.78%); **residual survived OOS** (IS 2.31% → OOS 3.54%;
  ATR IC 0.028→0.104). On these, the project is **NOT dead**.
- **But not skill either:** return rises **monotonically with realized volatility**
  (Low 1.25% / Med 1.69% / High 2.96%; β 0.82/1.0/1.18) — a volatility/risk-premium signature — and
  β-neutralisation removes only *market* beta, not *total/idiosyncratic* volatility. ATR is strongly
  **regime-dependent** (raw spread BULL 2.93% / NEUTRAL 0.2% / **BEAR 6.73%**, concentrated in bear
  mean-reversion) and carries deeper drawdown (High-vol MAE −8.4% vs Low −5.3%). **Sector** explains
  ~36% (within-sector spread 1.56% vs pooled 2.44%).
- **Decision (the branch this audit forces):** **Branch 2 — a factor survives OOS, so proceed to E4
  (regime gate) + re-test the frozen 2025–26 holdout.** *Caveat:* ATR is a **risk-premium harvest**
  (size for its drawdown), NOT free alpha. Branch 1 (ATR=beta → project dead) is **ruled out**.
- **Measurement only — no scoring/threshold/ranking/regime/factor logic changed; nothing tuned.**

### Phase K — Regime-Gated Leader Model (Repair-Lab E4, measurement only)
Module `analytics/e4_regime_gate.py`; report `reports/validation/e4_regime_gate_validation.md`. The
first *designed* logic change (Phase I §5, E4), executed as an A/B measurement on the cached replay
(NOT wired into production scoring). **One change only:** deploy the MARKET_LEADER basket *only* in
BULL weeks (cash in NEUTRAL/BEAR); the gate is the existing production regime label (BULL =
STRONG_BULL+BULL), fixed in advance — no threshold tuned, no parameter searched, leader definition
unchanged. Tested on the **frozen 2025–26 holdout** (75 weeks: 42 BULL · 11 NEUTRAL · 22 BEAR);
date-clustered bootstrap CIs + weekly equity-curve metrics reuse `trust_audit` verbatim.

- **Verdict: B) Small improvement.** The gate **flips OOS leader-excess −0.07% → +0.49%** and roughly
  **halves max drawdown (−20.0% → −8.9%)** + cuts vol (22.4% → 14.1%), but the gated excess **95% CI
  [−0.18, 1.08] still INCLUDES 0** (t=1.88, just under 1.96) → the pre-registered E4 criterion ("OOS
  excess > 0 with CI excluding 0 AND max DD reduced") **FAILS on the CI condition**.
- **Why drawdown falls but return doesn't (absolute vs relative):** OOS leader-excess by regime =
  BULL **+0.49%** (t=1.88), NEUTRAL +0.45% (t=0.74), **BEAR −1.51% (t=−3.38, reverses)** — confirming
  the gate's premise. But in BEAR the leaders were still mildly **positive absolutely (+1.6% 20D)**,
  lagging only a sharper universe/laggard bounce (+3.11%). So the gate removes a *relative*
  underperformance and its drawdown, **not an absolute loss** → deployed return ≈unchanged (both
  ~0.97×, slightly negative on the holdout); the gain is risk-side. (The CAGR/vol Sharpe proxy is
  uninformative when CAGR is ≈flat/negative; on the positive-return in-sample window the gate *does*
  lift Sharpe 2.89 → 3.12 and cuts IS max DD −19.0% → −12.6%.)
- **Net:** a real, unambiguous **risk overlay** (stands aside through the BEAR reversal that caused the
  un-gated OOS failure) whose **selection edge is directionally right but not statistically separable**
  on 2025–26 alone (42 BULL weeks = low power). Keep the gate; it does **not** by itself create a
  deployable edge. Carry the gated model into de-dilution (E1 drop-Breakout / E2 drop-Liquidity) + a
  transaction-cost model, then re-run the Trust Audit.
- **Measurement only — no production scoring/regime/ranking/factor logic changed; the gate was applied
  in the analytics replay, not in `build_screen`. Nothing tuned, searched or optimised.**

### Phase L — Real Portfolio State Layer (2026-06-14, production deployment layer)
New modules `portfolio/portfolio_state.py` + `deploy/order_card.py`; config `config/deployment.yaml`;
two new CLI flags `--sync-portfolio` and `--orders [--capital N]`. **No scoring, regime, ranking, or
factor logic changed.** This is a pure read-and-render layer sitting above the existing engines.

- **Problem addressed:** `--portfolio` outputs % allocations which are not actionable without capital,
  actual position quantities, or cash balance. The system had no awareness of the real broker state —
  it could not say "BUY 47 shares" or "you already hold 200 shares, no action needed."
- **`PortfolioState`** — new dataclass holding: total capital, holdings value, available cash, net equity,
  deployed %, cash %, and a `list[LiveHolding]` (ticker, symbol, sector, **quantity**, avg_price,
  current_price, current_value, cost_basis, unrealized PnL, allocation %, stop_price) + `list[OpenOrder]`.
- **`StateLoader`** — three sources in priority order: **(A) ZERODHA_LIVE** (`KiteClient.holdings_df()` +
  `margins_equity()`, stop prices enriched from last portfolio snapshot); **(B) MANUAL_CSV**
  (`state/holdings.csv` with ticker/qty/avg_price, prices fetched via yfinance); **(C) SIMULATED**
  (last `Journal/portfolio_history/*.json` rebuilt as quantities — the existing fallback).
- **`build_order_card(snap, state, capital, lc_snap)`** — diffs target (PortfolioSnapshot %) against
  current (PortfolioState quantities): new target position → BUY target_qty; lifecycle EXIT → SELL all;
  lifecycle REDUCE → SELL delta to target; lifecycle ADD → BUY delta to target; lifecycle ROTATE → SELL
  current (replacement appears as BUY via target slot); HOLD → hold unless drift > 2%; held but not in
  target → SELL (unless lifecycle says HOLD). Skips dust orders below ₹5,000.
- **`--sync-portfolio [--capital N]`** — syncs from Kite/CSV/simulated, renders a state summary table
  (symbol, qty, avg, LTP, P&L%, value, stop), saves to `state/portfolio_state.json`. Capital from
  `config/deployment.yaml` or `--capital` override.
- **`--orders [--capital N] [--refresh]`** — runs the full pipeline: `build_screen` → target portfolio
  (PortfolioConstructor) → load current state (load_state) → lifecycle review on live holdings →
  compute delta → render order card showing BUY/SELL/HOLD with exact share quantities, ₹ values, stops,
  cash required vs available, portfolio-after breakdown, "IGNORE ALL OTHER STOCKS," next review date.
- **`config/deployment.yaml`** — capital, target_positions, max_new_per_week, time_stop_weeks, posture
  (paper/small/full), min_trade_value, rebalance_threshold_pct. Posture defaults to `paper` (safe); card
  shows `[PAPER MODE — do not place real orders]` warning when posture ≠ full.
- **No scoring/regime/factor/ranking/portfolio math changed.**

## 3. Evidence Strong Enough To Treat As Fact
Supported by the 5-year validation/attribution:
- **RS leadership is predictive** — MARKET_LEADER > SECTOR_LEADER +0.93% (t=5.01); > LAGGARD. The strongest *directional* edge, but mainly a top-bucket signal (continuous corr ≈0.04).
- **Sector leadership is predictive** — +1.68%, monotonic, sign-stable across regimes, additive with RS.
- **Composite grade orders returns monotonically** (A+ → D) — edge concentrated in the extreme buckets, not as a continuous score.
- **Moderate-volatility (ATR) names outperformed** (+2.44%, every year) — real in-sample, but **most plausibly a volatility/beta exposure, not skill.**
- **Regime is useful for RISK, not direction** — it separates drawdown/win-rate (NEUTRAL/WEAK_BEAR worst) but its return ordering is inverted (STRONG_BEAR best).
- **Breakout and Liquidity components are net-negative / dilutive.**
- **ACTION_NOW is NOT yet statistically proven** (t=−0.96 vs AVOID, n=180).
- **EXTENDED outperformed** (t=8.41) — contradicts the "don't chase" design intent.

Robustness-qualified (Phase F) — the proven edge is REAL but FRAGILE, not robust:
- **The RS-leadership edge is broad across stocks and sectors** — survives removing the top-10 names
  (top-10 of 402 = only 26% of excess, t=3.33) and the strongest sector (Capital Goods, residual
  +0.7%, t=4.38). It is **not** carried by a handful of names or one sector.
- **…but it is time-concentrated and regime-dependent.** 58% of the leader excess comes from one
  2023-bull period; the edge **holds only in BULL (t=13.21)** and **reverses in BEAR (leaders lose to
  laggards, t=−3.25).** Treat it as a bull-regime signal, not an all-weather one.
- **ATR and Sector are the essential factors; RS is largely redundant inside an equal-weight blend
  and Liquidity is dilutive** (leave-one-out). ATR's importance carries the volatility/beta caveat.
- **Losing trades are not separable by the current scores** — worst-decile outcomes match the cohort
  factor profile and are simply high-drawdown (MAE ≈ −22%). The system cannot currently pre-identify
  its own failures.

Trust-qualified (Phase G) — the edge is in-sample-real but NOT yet deployment-grade:
- **It does NOT survive true out-of-sample.** In a chronological 2025 holdout the MARKET_LEADER edge
  is **−0.14%** (fails) and ACTION_NOW **−2.88%** (fails); 2026 leader edge only +0.11%. The measured
  edge is concentrated in 2022–2024 (esp. 2023) and did not appear in the unseen year.
- **Bootstrap CIs confirm the split:** MARKET_LEADER − universe **+1.06% CI [0.68, 1.45] (real)**;
  **ACTION_NOW − AVOID +0.90% CI [−0.99, 2.91] — includes 0, i.e. plausibly noise.**
- **Deployed in-sample, the Top-10 basket has been underwater for the trailing ~100 weeks** (max DD
  −35.6%, gross of costs) — the 2023 high-water mark was never reclaimed.
- **Overall Trust Score 53/100 → "Research Only"; personal-trust verdict PARTIALLY** (trust the
  leader signal as research, not the system for capital). See `reports/validation/trust_audit.md`.

Why-it-failed (Phase H forensics):
- **The edge was period-specific, not overfit and not a clean decay.** Verdict **C**. The OOS failure
  is ~59% market/regime-and-sector composition shift + ~41% within-group selection decay; the leaders'
  core ranking scores barely moved (5.7 pts) yet **RS and Trend factor ICs inverted out-of-sample**,
  while ATR was the only factor that held. The market changed AND the scoring stopped predicting —
  both at once. See `reports/validation/oos_failure_forensics.md`.

## 4. Questions — Phase 4 Resolutions + Remaining Unknowns

**Answered by Phase 4 (robustness audit):**
- **Robustness across sub-periods** → **time-UNSTABLE / concentrated.** Positive in 4/5 periods but
  58% of leader excess is from one 2023-bull window; **not** a 2022 bear-bounce artifact (P1 weak).
- **Sector concentration** → **NOT concentrated.** Edge survives removing the strongest sector
  (Capital Goods → residual +0.7%, t=4.38).
- **Stock concentration** → **NOT concentrated for leaders** (top-10 of 402 = 26% of excess);
  **IS concentrated for ACTION_NOW** (top-10 of 112 = 62%).
- **Factor dependency** → essential factors are **Sector + ATR**; **RS is redundant** in an
  equal-weight blend; **Liquidity dilutes**; Breakout ~neutral.
- **Regime dependence** (new) → edge **holds only in BULL, reverses in BEAR** (leaders < laggards,
  t=−3.25). Verdict: **B) Promising but fragile.**

**Still open (NOT addressed by Phase 4 — measurement only, no new method run):**
- ~~**Is ATR's edge real or just beta?**~~ **RESOLVED (Phase J / 6A):** ATR is **E) Mixed** — a
  **volatility/risk-premium** factor that survives market-beta neutralisation (keeps ~80% of its
  spread) and survives OOS, but is **not proven skill** and not pure market-beta. See §2 Phase J, §4f.
- **Survivability in future / unseen markets** — partly addressed by the 2025–26 holdout (Phases
  G/H/K): the un-gated edge fails OOS; the BULL-gated edge is positive (+0.49%) but its CI includes 0
  (low power). The 2023-concentration remains a red flag; durability is still unproven.
- **Why does EXTENDED beat ACTION_NOW, and is it robust?** — not re-examined here.
- **Can ACTION_NOW ever be proven** given n=180 (~36/period)? Phase 4 does not elevate it; it remains
  under-powered and, unlike the leader cohort, *concentrated* in a few names.
- **Can failures be pre-identified?** Phase 4 says **no** with current scores — an open modelling gap.

## 5. Constraints For Future Development
1. **No scoring changes without evidence.**
2. **No threshold changes without measurement.**
3. **No optimization before a robustness audit.**
4. **Missing data must never become bearish data.**
5. **Validation precedes modification.**

## 6. Current State
- **Completed:** retirement audit · `--today` demotion · data resilience · 5-year validation ·
  edge attribution · **robustness audit (Phase 4 — verdict B, Promising but fragile)** ·
  **trust validation audit (Phase G — Trust Score 53/100 "Research Only", verdict PARTIALLY)** ·
  **OOS failure forensics (Phase H / Phase 5 — verdict C, Edge was period-specific)** ·
  **System Repair Lab (Phase I / Phase 6 — DESIGN ONLY; repair plan written, nothing implemented)** ·
  **ATR Alpha-vs-Beta Audit (Phase J / Phase 6A — verdict E) Mixed; E5 DONE)** ·
  **Regime-Gated Leader Model (Phase K / Repair-Lab E4 — verdict B) Small improvement; E4 DONE)** ·
  **Real Portfolio State Layer (Phase L — capital-aware deployment engine; no scoring changes).**
- **Pending / next:** **Phase L (deployment layer) is now DONE.** The system can now output capital-aware
  order cards with exact share quantities, stops in ₹, cash required vs available, and a "IGNORE ALL
  OTHER STOCKS" weekly workflow via `--sync-portfolio` + `--orders --capital N`. **Immediate next steps
  (no scoring change, ship now):** (1) set capital in `config/deployment.yaml`; (2) run
  `--sync-portfolio` weekly after Kite login; (3) run `--orders` to get the week's trade list.
  **Research critical path (unchanged):** de-dilution hygiene (E1 drop-Breakout → E2 drop-Liquidity
  → E3 drop-Trend/Freshness) to try to lift the gated OOS edge CI off zero, then a
  **transaction-cost / turnover model**, then **re-run the Trust Audit** on the gated+de-diluted model.
  ATR is a **volatility/risk-premium** factor, not skill. Each step is a pre-registered one-change A/B;
  **none may be adopted into production until it passes out-of-sample.**
- **Status:** Research complete through E4; the regime gate is *validated as a measurement* but **not
  wired into the pipeline.** **Nothing committed.** Uncommitted in the working tree: the data-resilience
  reliability fix (the only *production logic* change so far — verified 13/13, benchmark-present
  invariant holds), the `--today` demotion (display/doc), and the measurement modules + reports
  (`analytics/action_now_validation.py`, `analytics/edge_attribution.py`, `analytics/robustness_audit.py`,
  `analytics/trust_audit.py`, `analytics/oos_forensics.py`, `analytics/atr_alpha_beta.py`,
  `analytics/e4_regime_gate.py` + `reports/validation/*`, incl. the design doc `system_repair_lab.md`).
  **No production scoring / regime / ranking math is changed anywhere — the Repair Lab, the ATR audit
  and the E4 gate changed nothing in `build_screen`; E4's gate lives only in the analytics replay.**

## 7. Phase 4 Outcome (COMPLETE)
- **Inputs used:** `reports/validation/action_now_5y.md`, `reports/validation/edge_attribution_audit.md`.
- **Deliverable:** `reports/validation/robustness_audit.md` (module `analytics/robustness_audit.py`;
  cached dataset `reports/validation/robustness_dataset.csv`). Re-run report-only with `--reuse`.
- **Verdict: (B) Promising but fragile.** The RS-leadership / composite-extreme edge is **real and
  broad-based** (survives stock and sector jackknifes) but **fragile in time and regime** — 58% of the
  excess is one 2023-bull period and the edge **reverses in BEAR**. ACTION_NOW stays unproven (n=180).
- **Measurement only — no tuning, no optimization, no modifications were made.**
- **Do not jump to Phase 5.** Any scoring/threshold/factor/regime change still requires an explicit
  mandate; the constraint "validation precedes modification" (§5) is unchanged.

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

### 4b. 5-Year forensic validation + edge attribution (2026-06-13, measurement only)

Higher-n replay (**218 weekly screens, 94,151 obs, 2022-04 → 2026-05**); production engines
replayed point-in-time, **no logic changed**. Reports: `reports/validation/action_now_5y.md`,
`reports/validation/edge_attribution_audit.md` (modules `analytics/action_now_validation.py`,
`analytics/edge_attribution.py`).

| Finding (20D) | Status at 5y | Evidence |
|---|---|---|
| RS leadership (MARKET_LEADER > SECTOR_LEADER) | ✅ **CONFIRMED** | +0.93%, **t=5.01** (ML +3.03% vs LAGGARD +1.45%) |
| Actionability (ACTION_NOW > AVOID) | 🟡 **STILL UNPROVEN** | +0.89%, **t=−0.96**, n only **180** (rare class) |
| Composite grade (A+→D) | ✅ **monotonic**, but corr ≈0.04 | A+ +4.45% → D +1.54%; edge lives in the extremes |
| EXTENDED vs universe | ⚠️ **OUTPERFORMS** (vs design intent) | +3.85%, **t=8.41** — "don't chase" label captures continuation |
| Regime → forward returns | ❌ **inverted/non-monotone** | STRONG_BEAR +5.3% best, NEUTRAL −1.0% worst; regime useful for **drawdown/win, not direction** |

**Attribution (independent Q5−Q1 20D spread):** strongest = **ATR +2.44%** (stable, but likely a
**volatility/beta** exposure — flips negative in WEAK_BEAR) and **Sector +1.68%** (monotonic,
additive with RS). **Net-negative / dilutive:** **Breakout −1.06%** (negative every year) and
**Liquidity −1.76%** — both DILUTE the RS signal. **Redundant:** Trend ≈ RS. **Noise:** Freshness.
The composite blend (+1.25%) scores **below ATR-alone and Sector-alone** — i.e. it is diluted by
its own weighting (Breakout 20% / Liquidity 10% vs ATR 5%). *Recorded as evidence; no change made.*

### 4c. Robustness audit (Phase 4, 2026-06-13, measurement only)

Six durability lenses on the same 94,637-obs replay (`analytics/robustness_audit.py`,
`reports/validation/robustness_audit.md`). **Verdict: B) Promising but fragile — 3/5 checks.**

| Lens | Result | Evidence |
|---|---|---|
| Time stability | ⚠️ **UNSTABLE / concentrated** | leader excess +0.46/+3.45/+1.50/−0.28/+0.53% (P1→P5); 58% from the 2023-bull period; **not** a 2022 bounce |
| Regime stability | ❌ **BULL-only; reverses in BEAR** | Leader−Laggard: BULL +2.32% (t=13.21) · NEUTRAL +0.48% (t=0.95) · BEAR **−1.24% (t=−3.25)** |
| Sector dependency | ✅ **broad** | remove Capital Goods → residual leader-excess +0.7% (t=4.38) |
| Stock dependency | ✅ **broad (leaders)** / ⚠️ concentrated (ACTION_NOW) | top-10 of 402 leaders = 26% of excess (t=3.33 after removal); ACTION_NOW top-10 of 112 = 62% |
| Factor dependency | **Sector + ATR essential**; RS redundant; Liquidity dilutive | equal-weight LOO Δ: ATR +0.81, Sector +0.56, RS +0.15, Liquidity −0.54 |
| Failure clusters | not separable by score | worst-decile ≈ cohort factor profile; only distinguished by drawdown (MAE ≈ −22%) |

*Recorded as evidence; no scoring/threshold/factor/regime/ranking change made; no recommendations.*

### 4d. Trust validation audit (Phase G, 2026-06-13, measurement only)

Seven trust lenses on the same dataset (`analytics/trust_audit.py`, `reports/validation/trust_audit.md`).
**Trust Score 53/100 → "Research Only"; personal-trust verdict: PARTIALLY.**

| Lens | Result | Evidence |
|---|---|---|
| Out-of-sample (walk-forward) | ❌ **FAILS 2025 holdout** | MARKET_LEADER eval-excess −0.14%, ACTION_NOW −2.88%; 2026 leader only +0.11% |
| Decision quality | ✅ sorts better than no-skill (in-sample) | Top-10 3.21% vs random-10 1.62% vs universe 1.96% |
| Confidence intervals | leader REAL, ACTION_NOW noise | ML−uni +1.06% CI [0.68,1.45]; **ACTION_NOW−AVOID +0.90% CI [−0.99,2.91] includes 0** |
| Stability | survivable, long droughts | 34/50 months +; longest losing streak 4mo, underperformance 5mo |
| Deployment sim (in-sample, gross) | ⚠️ underwater since 2023 peak | Top-10 max DD −35.6%, trailing ~100 wks below high-water mark |
| Failure modes | not pre-identifiable within regime | fail-rate ~0.43 flat across regimes; anticipatable by regime only |
| Trust Score | **53/100 "Research Only"** | DataInteg 98 · Stat 70 · Robust 55 · OOS 33 · Deploy 27 |

*Recorded as evidence; nothing tuned, optimised, redesigned or changed; no recommendations.*

### 4e. OOS failure forensics (Phase H / Phase 5, 2026-06-14, measurement only)

Why the 2025 holdout failed (`analytics/oos_forensics.py`, `reports/validation/oos_failure_forensics.md`).
**Verdict: C) Edge was period-specific.**

| Lens | Finding |
|---|---|
| Edge decay (§1) | leader excess by yr +0.76/+3.28/+1.06/−0.14/+0.11% — peaked 2023, **non-monotone**, COLLAPSED in 2025 |
| Market structure (§2) | BULL share 0.87→0.56, BEAR 0.09→0.29, mean trend 58→40 — **the market shifted** |
| Leadership quality (§3) | core RS/sector/trend drift only 5.7 pts, yet leader excess +1.72%→−0.07% → scoring stopped picking winners |
| Factor drift (§4) | **RS and Trend ICs INVERTED OOS**; ATR the only factor that held |
| ACTION_NOW (§5) | IS +4.94% → OOS −1.01%; scores unchanged (no inflation) → noise + regime mismatch |
| Sector shift (§6) | Capital Goods leader return 6.39%→1.97%, share 0.25→0.18 |
| Decomposition (§7) | ΔE −1.79% = regime −0.46 + sector −0.59 + selection −0.74 (**~59% market-shift, ~41% selection decay**) |
| Counterfactual (§8) | gain confidence 3/5 yrs (2022–24); lose in 2025 & 2026 (negative CAGR) |

*Recorded as evidence; nothing changed; explains the failure, proposes no fix.*

### 4f. ATR alpha-vs-beta audit (Phase J / Phase 6A, 2026-06-14, measurement only)

Is the only OOS-durable factor real? (`analytics/atr_alpha_beta.py`,
`reports/validation/atr_alpha_beta_audit.md`). **Verdict: E) Mixed — volatility/risk-premium, survives
OOS, not skill, not pure market-beta.**

| Lens | Finding |
|---|---|
| Vol cohorts (§1) | return rises monotonically with realized vol: Low 1.25% / Med 1.69% / High 2.96% (β 0.82/1.0/1.18) — risk-premium signature |
| Beta correlation (§2) | ATR↔β(N50) Pearson **0.216** (weak); rvol↔β 0.535 — ATR *score* is not a simple beta proxy |
| Residual (§3) | β-neutral spread 1.96% vs raw 2.44% → keeps **~80%**; beta explains ~20% |
| Sector (§4) | within-sector spread 1.56% vs pooled 2.44% → sector ~36% |
| Regime (§5) | raw spread BULL 2.93% / NEUTRAL 0.2% / **BEAR 6.73%** → strongly regime-dependent |
| Interaction (§6) | adds beyond RS (0.51→2.33) and Sector (1.68→2.78) |
| Holdout (§7) | residual spread IS 2.31% → **OOS 3.54%** (survived); IC 0.028→0.104 |

**Branch decision:** Branch 2 (a factor survives OOS) → proceed to E4 regime-gate; treat ATR as a
risk-premium harvest (size for drawdown), not free alpha. Branch 1 (project dead) ruled out.
*Recorded as evidence; nothing changed.*

### 4g. Regime-gated leader model (Phase K / Repair-Lab E4, 2026-06-14, measurement only)

Does gating the leader edge to BULL fix the OOS failure? (`analytics/e4_regime_gate.py`,
`reports/validation/e4_regime_gate_validation.md`). One change — deploy MARKET_LEADER only in BULL
weeks, cash otherwise — on the frozen 2025–26 holdout. **Verdict: B) Small improvement** (pre-registered
E4 criterion FAILS on the CI condition).

| Lens (OOS 2025–26) | Original (un-gated) | Gated (BULL only) |
|---|--:|--:|
| Leader-excess (20D, vs universe) | **−0.07%** (CI [−0.62,0.42]) | **+0.49%** (CI **[−0.18,1.08] incl. 0**, t=1.88) |
| Deployed max drawdown | −19.99% | **−8.88%** |
| Annualised vol | 22.4% | **14.1%** |
| Deployed growth / CAGR | 0.97× / −2.16% | 0.97× / −2.28% (≈unchanged) |
| Exposure (weeks invested) | 0.99 | 0.56 |

- OOS leader-excess by regime: BULL **+0.49% (t=1.88)** · NEUTRAL +0.45% (t=0.74) · **BEAR −1.51%
  (t=−3.38, reverses)** — the gate's premise holds; the BEAR weeks are what sink the un-gated edge.
- **Gate cuts risk, not an absolute loss:** in BEAR the leaders were still mildly **positive (+1.6%)**,
  lagging only a sharper universe bounce (+3.11%) — so standing aside removes a *relative*
  underperformance + drawdown but forfeits a small positive, leaving deployed return ≈flat either way.
- **Net:** proven **risk overlay**; selection edge directionally right but **not statistically separable**
  on this holdout. Keep the gate; not a deployable edge alone. *No production scoring/regime/ranking
  logic changed — the gate was applied in the analytics replay, not in `build_screen`.*

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
**Old workstation (LEGACY — `--today` DEMOTED to research/debug, RC1 retirement audit):**
`--today --positions --place --morning --backtest --algo --orb-screen --kite-login
--doctor --reconcile` (separate system; see §3b). **Do not use `--today` for stock
discovery** — it sees only 101 static names (0% of the validated screen's Top-10 leaders)
and shows no incremental alpha. Route discovery through `--screen`. Execution commands
(`--place`, `--positions`, `--reconcile`, …) are retained until ported. Full evidence:
`RETIREMENT_AUDIT.md`.

**Audits (RC1):**
```
python verification/release_candidate.py    # full E2E system audit (15 checks)
python audit/consistency_report.py          # cross-subsystem consistency (5 checks)
python verification/phase{1..10b}.py        # per-phase acceptance (each 8/8)
python verification/rolling_validation.py   # rolling framework acceptance
python verification/data_resilience.py      # benchmark-outage resilience (13/13)
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
- **Data resilience hardening (2026-06-12, reliability-only):** fixed a critical bug where a
  missing `^NSEI` benchmark forced every stock to NEUTRAL → false WEAK_BEAR / 0 ACTION_NOW
  ("missing data = bearish data"). Now: RS honours its sector-relative fallback; benchmarks
  are cached (`cache/benchmarks/`, live→cache→missing); cockpit shows a DATA QUALITY verdict
  (HEALTHY/DEGRADED/FAILED); and on total `^NSEI` loss the screen **fails loud / aborts**
  (`build_screen(allow_degraded=True)` to override). **No scoring/regime/composite/
  actionability/portfolio math changed.** Verified 13/13. Full record: `DATA_RESILIENCE_AUDIT.md`.
- **5y validation + edge attribution (2026-06-13, measurement only):** replayed 218 weekly
  screens / 94,151 obs; see §4b. New research modules `analytics/action_now_validation.py` +
  `analytics/edge_attribution.py`; reports under `reports/validation/`. Headlines: RS-leadership
  edge confirmed (t=5.01); ACTION_NOW still unproven (n=180); EXTENDED outperforms; Breakout &
  Liquidity are net-negative/dilutive; ATR (likely beta) + Sector are the strongest predictors;
  regime is informative for risk, not direction. **No scoring/logic changed.**
- **Robustness audit / Phase 4 (2026-06-13, measurement only):** new research module
  `analytics/robustness_audit.py` → `reports/validation/robustness_audit.md` (dataset cached to
  `reports/validation/robustness_dataset.csv`; report-only re-run via `--reuse`). Six durability
  lenses on the 94,637-obs replay. **Verdict: B) Promising but fragile (3/5 checks):** the
  RS-leadership edge is **broad across stocks and sectors** but **time-concentrated (58% in one
  2023-bull window) and regime-fragile (holds in BULL, REVERSES in BEAR, t=−3.25);** Sector+ATR are
  the essential factors, Liquidity dilutes; failures aren't separable by score. **No scoring/logic
  changed.** See §2 Phase F, §4c. Next natural measurements: beta-neutral ATR test + a true
  out-of-sample / walk-forward holdout (NOT modifications).
- **Trust validation audit / Phase G (2026-06-13, measurement only):** new research module
  `analytics/trust_audit.py` → `reports/validation/trust_audit.md` (runs off the cached dataset;
  `--rebuild` to re-replay). Seven trust lenses answering "is this deployment-grade?" **Trust Score
  53/100 → "Research Only"; verdict PARTIALLY.** Decisive finding: the edge **FAILS the 2025
  out-of-sample holdout** (MARKET_LEADER −0.14%, ACTION_NOW −2.88%); the in-sample Top-10 basket has
  been underwater since its 2023 peak; ACTION_NOW−AVOID bootstrap CI includes 0 (noise); only the
  MARKET_LEADER edge has a CI excluding 0. The RS-leadership signal is real and broad in-sample but
  not yet durable out-of-sample. **No scoring/logic changed.** See §2 Phase G, §4d.
- **OOS failure forensics / Phase H (Phase 5, 2026-06-14, measurement only):** new research module
  `analytics/oos_forensics.py` → `reports/validation/oos_failure_forensics.md` (off the cached
  dataset; `--rebuild` to re-replay). Nine forensic lenses explaining WHY the holdout failed.
  **Verdict: C) Edge was period-specific.** ΔLeader-excess IS→OOS −1.79% is ~59% market/regime-sector
  composition shift (BULL share 0.87→0.56) and ~41% within-group selection decay — the leaders' core
  ranking scores barely moved (5.7 pts) yet **RS and Trend factor ICs inverted out-of-sample** and
  those names stopped winning. Not overfit (broad in-sample), not monotone decay (excess spiked in
  2023). **No scoring/logic changed; explains the failure, proposes no fix.** See §2 Phase H, §4e.
- **System Repair Lab / Phase I (Phase 6, 2026-06-14, DESIGN ONLY):** report
  `reports/validation/system_repair_lab.md` (no module — design doc). First phase where logic changes
  are *permitted to be designed*; **none were implemented and nothing was tuned.** Smallest path from
  "Research Only" toward "Potentially Deployable": KEEP Sector + RS (regime-gated top-bucket) + Regime
  (as a gate), INVESTIGATE ATR (alpha vs beta), REMOVE Breakout/Liquidity/Trend/Freshness from
  scoring; min-viable model = Sector·RS·ATR (→ Sector+RS if ATR is beta). Critical path = **E5 ATR
  beta test → E4 regime-gate → re-test frozen 2025–26 holdout**, each a pre-registered one-change A/B.
  **No scoring/regime/ranking math changed.** See §2 Phase I.
- **ATR Alpha-vs-Beta Audit / Phase J (Phase 6A, 2026-06-14, measurement only):** new module
  `analytics/atr_alpha_beta.py` → `reports/validation/atr_alpha_beta_audit.md` (beta read directly
  from `cache/ohlcv/*_5y.csv` + `cache/benchmarks/NSEI_5y.csv`, offline; per-ticker beta cached,
  `--reuse-beta`). **Verdict: E) Mixed** — ATR is a **volatility/risk-premium** factor: not a simple
  market-beta proxy (β explains ~20%, residual keeps ~80%, ATR↔β r=0.216, adds beyond RS & Sector,
  **survived OOS** 3.54%), but not proven skill (monotone vol→return, regime-dependent; β-neutralisation
  removes market beta only). **Branch 2** (a factor survives OOS) → next is E4 regime-gate; treat ATR
  as a risk premium, size for drawdown. **No scoring/logic changed.** See §2 Phase J, §4f.
- **Regime-Gated Leader Model / Phase K (Repair-Lab E4, 2026-06-14, measurement only):** new module
  `analytics/e4_regime_gate.py` → `reports/validation/e4_regime_gate_validation.md` (off the cached
  dataset; `--rebuild` to re-replay). One pre-registered change — deploy the MARKET_LEADER basket only
  in BULL weeks (cash in NEUTRAL/BEAR), gate = existing regime label, fixed in advance — A/B-tested on
  the frozen 2025–26 holdout (75 weeks: 42 BULL/11 NEUTRAL/22 BEAR). **Verdict: B) Small improvement.**
  The gate flips OOS leader-excess **−0.07%→+0.49%** and halves max DD **−20.0%→−8.9%** (vol 22.4→14.1%),
  but the gated excess CI **[−0.18,1.08] includes 0** (t=1.88) → pre-registered E4 success criterion
  FAILS on the CI; deployed return ≈unchanged (BEAR leaders were +1.6% absolute, lagging only
  relatively, so the gate cuts drawdown, not a loss). A proven **risk overlay**, not yet a deployable
  edge. **No production scoring/logic changed — the gate lives only in the analytics replay, not
  `build_screen`.** See §2 Phase K, §4g.
- **`--today` demotion (2026-06-12, display/doc only):** RC1 retirement audit found `--today`
  covers 0% of the validated screen's Top-10 leaders (12.5% of all leaders) and shows no
  incremental alpha; demoted to research/debug. Discovery = `--screen`. Added a deprecation
  banner (`daily_mode._print_deprecation_banner`) + help/HANDOFF notes. **No scoring/weights/
  factors/regime/portfolio logic changed.** Full evidence: `RETIREMENT_AUDIT.md`.
- **Recommended git action:** commit on a branch, then
  `git tag -a v1.0-RC1 -m "Dynamic swing screener — RC1 freeze"`.
- **Freeze rule:** no new features / indicators / scoring formula / weight changes until a
  new session explicitly reopens development. Next focus = collect live weekly snapshots and
  let the rolling validator accumulate evidence.

_Update §4 (findings) and §10 (state) at the end of every session._
