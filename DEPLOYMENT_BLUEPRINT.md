# Deployment Blueprint — turning the research engine into a weekly, low-fatigue trading system

_Authored 2026-06-14. Practical design doc, not an audit. It assumes the evidence in
`HANDOFF.md` and `reports/validation/*` and does **not** re-open any of it. Goal: a system a
human can run in **< 10 minutes/week** that answers buy-what / how-much / when / when-to-sell —
with the decisions pre-made by rules, not by the trader._

> **Read first — the one honest constraint.** A deployment layer can remove *decision fatigue*
> today (a solved engineering problem). It **cannot** manufacture an out-of-sample edge that the
> evidence says is not yet proven (Trust 53/100 "Research Only"; the un-gated leader edge fails the
> 2025–26 holdout; ACTION_NOW is noise; E4's BULL gate cut drawdown but its OOS edge CI still
> includes 0). So this blueprint is built to be **deterministic and regime-gated**, and it
> deliberately rejects "0% cash always" — that contradicts our own E4 result (standing aside in
> BEAR roughly halved drawdown). The system decides *when to hold cash*; that is a feature, not
> fatigue. **Deploy posture: paper or small (≤ 25–30% of intended capital) until a live forward
> track record + a transaction-cost-aware Trust re-score support scaling up.**

> **What already exists (don't rebuild it).** Most of the deployment engine is already written:
> - `python run.py --portfolio` (Phase 9, `portfolio/portfolio_engine.py`) → conviction-weighted,
>   **risk-based** position sizing with a **regime exposure cap** (STRONG_BULL 100% … STRONG_BEAR
>   10%), sector 30% / correlation-cluster 20% / max-8 / max-10%-per-name limits, ATR-based stops,
>   cash %. It already outputs *today's portfolio* — just in **percent**, not rupees.
> - `python run.py --review-portfolio` (Phase 10B, `portfolio/lifecycle_engine.py`) →
>   **HOLD / ADD / REDUCE / EXIT / ROTATE** with rule-based, non-discretionary exits + theme cap.
>
> The gap is **presentation + policy reconciliation**, not a missing engine.

---

## 1. Problems preventing deployment today

Ranked by how much each one actually blocks "run it Friday with real money."

| # | Problem | Why it blocks deployment | Fixable by |
|---|---|---|---|
| 1 | **Output is analysis, not orders.** `--portfolio` prints % allocations + 9 columns; no rupee amounts, no "ignore everything else," no "next review date." | The trader still has to translate, decide, and second-guess → fatigue. | §10 Step 1 (thin presenter) |
| 2 | **Eligibility/conviction encode pre-validation beliefs.** Positions are made eligible by `ACTION_NOW`/`WATCHLIST` (ACTION_NOW = proven noise) and conviction weights actionability/RR/grade (weak signals), not the durable RS-leadership + sector + regime evidence. | The book can be selected partly on factors the audits showed are noise/dilutive. | §10 Step 3 (requires an OOS A/B — it's a scoring change) |
| 3 | **No explicit hard regime gate / no fixed N / no time-stop.** Exposure scales by regime (good) but there's no "BEAR ⇒ effectively flat" switch surfaced as a rule, no fixed position count for a low-fatigue feel, and no maximum holding period. | The trader can't see the gate; turnover and "dead money" positions accumulate. | §10 Step 2 (config + rules, no scoring change) |
| 4 | **Edge is not OOS-proven (the real constraint).** Research-Only; the leader edge is bull-regime-dependent and time-concentrated. | Caps how much capital is justified, not whether the workflow can run. | Posture (paper/small) + the queued E1/E2 + cost model, then Trust re-score |
| 5 | **No transaction-cost / turnover model.** Every backtest is gross of costs; weekly rebalancing has real slippage/brokerage/STT. | Unknown whether the thin edge survives costs. | A cost model (separate, already flagged in Repair-Lab §7 blocker #5) |

**Problems 1 and 3 are pure plumbing/policy and ship now. Problem 2 is a scoring change — it must
pass its pre-registered OOS A/B (E1/E2/E7) before adoption, per the project's standing rule
"validation precedes modification." Problems 4–5 set the *capital* dial, not the workflow.**

---

## 2. Minimum viable deployment model

The smallest thing that is *runnable Friday* and *honest*:

```
ONE leader basket, regime-gated, risk-sized, weekly-reviewed.

  Selection : RS leaders (MARKET_LEADER / SECTOR_LEADER) in leading sectors, liquid enough to trade.
  Sizing    : risk-based — each name risks a fixed % of capital to its ATR stop (size DOWN volatile names).
  Count     : target 5 names (low-fatigue), engine may hold up to 8 in STRONG_BULL, 0–2 in BEAR.
  Regime    : exposure scales with regime; BEAR ⇒ near-cash (the E4 gate, already in the engine).
  Manage    : weekly — EXIT/ROTATE/REDUCE by rule; new BUYs fill empty slots.
  Posture   : paper or ≤25–30% of intended capital until a live track record earns scale-up.
```

This is **Model "B+D" from the Repair Lab** in deployable form: a de-emphasis of the dilutive
factors (Problem 2, pending its A/B) plus the regime gate (E4, validated as a risk overlay). It does
**not** claim alpha; it claims a disciplined, low-fatigue way to express the one signal that is real
in-sample (RS leadership) while standing aside when the evidence says it reverses (BEAR).

Everything below is the rule-set that makes this runnable with near-zero thinking.

---

## 3. Portfolio rules

| Knob | Rule | Where it lives | Evidence basis |
|---|---|---|---|
| **How many positions** | Target **5**; allow **up to 8** in STRONG_BULL, **0–2** in BEAR. | `MAX_POSITIONS` (engine = 8 → set effective target 5) | low fatigue; broad-not-concentrated leader edge (top-10 of 402 = 26% of excess) |
| **Weighting** | **Risk-based, not equal-weight**: each name risks ≤ 1% of capital to its stop ⇒ wider ATR stop = smaller position. Per-name **hard cap 10%**. | `PositionSizer.size_by_risk`, `MAX_POSITION_SIZE` | ATR = risk premium → equalise *risk*, don't over-allocate the most volatile names |
| **Cash allocation** | **Regime-scaled, not 0%.** Deployed exposure cap = STRONG_BULL 100 / BULL 75 / NEUTRAL 50 / WEAK_BEAR 25 / STRONG_BEAR 10. Remainder = cash. | `REGIME_EXPOSURE` | E4: cash in BEAR ≈ halved drawdown; leaders reverse in BEAR (t≈−3.3) |
| **Max sector** | **30%** of capital in any one sector. | `MAX_SECTOR_EXPOSURE` | sector edge is broad; avoid single-sector blow-up (Capital Goods carried failures) |
| **Max theme** | **40%** across a macro theme (e.g. Capital Goods+Power+Construction = one bet). | `MAX_THEME_EXPOSURE`, `config/theme_map.yaml` | correlated leadership concentrates risk |
| **Max correlation cluster** | **20%** in any 60D-corr>0.80 cluster (treat as one trade). | `MAX_CLUSTER_EXPOSURE` | avoids hidden single-bet leverage |
| **Min position** | Skip anything < **1.5%** (dust). | `MIN_POSITION_SIZE` | operational |

All seven are **already enforced** by `PortfolioConstructor`. The deployment policy keeps them and
only changes the *target count feel* (5) and makes the cash rule explicit to the trader.

---

## 4. Buy rules

| Question | Rule |
|---|---|
| **Which candidates become buys** | The constructed portfolio's positions (RS leaders passing all §3 limits) that you do **not** already hold. Today's engine selection *is* the buy list. |
| **How many buys per week** | Only enough to fill empty slots up to the regime's target count, **capped at 2–3 new names/week** to bound turnover and cost. Never churn the whole book. |
| **Ranking / selection method** | **Conviction**, anchored on the proven edge: RS-leadership first, sector leadership second, entry-quality only as a tie-breaker. (Today's engine conviction = `0.45·RS + 0.35·actionability + 0.20·RR ± grade`; the evidence-aligned re-anchoring to RS+Sector is Problem 2 / §10 Step 3, pending its OOS A/B.) |
| **Hard pre-conditions (all must hold)** | (a) regime not STRONG_BEAR-flat; (b) RS status ∈ {MARKET_LEADER, SECTOR_LEADER}; (c) liquidity above the tradability floor; (d) the name's sector/theme/cluster is not already at cap. |
| **What to ignore** | EXTENDED/AVOID classes for *new* capital; anything below the liquidity floor; any name that only screens well on Breakout/Liquidity/Trend/Freshness (dilutive/noise). |
| **Entry mechanics** | Buy at next session open (or the engine's `entry`); set the stop in the **same order** (§6). No staring at intraday charts. |

---

## 5. Hold rules

| Question | Rule |
|---|---|
| **Expected holding period** | **4–12 weeks** (the validated 20D–60D forward horizon). Don't expect day-trade tempo. |
| **Review cadence** | **Once per week** (weekend / Friday close) — the cadence the screener runs and persists. No mid-week tinkering unless a stop is hit. |
| **When to keep holding** | While the lifecycle status is **HOLD or ADD**: RS still ∈ leader tier, grade not D, trend intact, stop not hit, and no much-stronger same-theme replacement. |
| **Adding** | Only in non-BEAR regimes with theme/exposure room, and only to **leaders** on a pullback (or to pyramid a leader in STRONG_BULL). Never average **down** a loser. |

All encoded in `ExitAnalyzer`/`AddAnalyzer`; surfaced by `--review-portfolio`.

---

## 6. Exit rules

Exits are **rule-based and non-discretionary** (already in `ExitAnalyzer`), with two additions:

| Trigger | Action | Source |
|---|---|---|
| **Hard ATR stop hit** (price ≤ stop) | EXIT in full | engine stop, set at entry |
| **RS collapses to LAGGARD** | EXIT | `ExitAnalyzer` |
| **Composite grade collapses to D** | EXIT | `ExitAnalyzer` |
| **Trend broken / drops out of screen universe** | EXIT | `ExitAnalyzer` |
| **Regime turns bearish** | STRONG_BEAR ⇒ exit all but elite leaders; WEAK_BEAR ⇒ trim laggards to raise cash | `ExitAnalyzer` (regime-tightened) |
| **Over-extension** (> 15% above breakout/EMA20; 20% in STRONG_BULL) | REDUCE (profit-take a portion, trail the rest) | `_is_reduce` |
| **Opportunity cost** (same-theme leader beats held conviction by > 15) | ROTATE into the stronger name | `RotationAnalyzer` |
| **➕ Time stop (new)** | If a position is **flat-to-negative after ~12 weeks**, EXIT — capital is doing nothing. | §10 Step 2 (add) |
| **➕ Profit-taking ladder (optional, new)** | Trail the stop up (e.g. to breakeven at +1R, then ATR-trail). Keeps winners, caps give-back. | §10 Step 2 (add) |

**No discretionary exits.** If none of the above fires, you hold. That is the anti-fatigue core.

---

## 7. Weekly workflow (target < 10 minutes)

```
ONCE A WEEK  (Friday close or weekend)

1.  python run.py --review-portfolio        # ~what do I already hold?
       → Act on EXIT and ROTATE rows first (sell / replace). REDUCE = trim. HOLD/ADD = leave.

2.  python run.py --portfolio               # ~what is today's target book?
       → (with the §10 presenter) prints a rupee ORDER CARD: BUY list + amounts,
         cash %, "ignore everything else", next review date.

3.  Place the handful of orders:
       a. EXITs / ROTATE-sells   b. new BUYs (≤ 2–3) with stop attached
       → Done. Walk away until next week.
```

No charts, no rankings to interpret, no "should I?". The two commands + a few orders is the entire
job. In BEAR weeks the order card will say *hold cash* — that is the system working, not a missing
signal.

---

## 8. Risk management

| Layer | Rule | Rationale |
|---|---|---|
| **Per-trade risk** | ≤ **1%** of capital to the stop (conviction-scaled down for weaker names). | survive losing streaks (longest historical losing streak = 4 months) |
| **Portfolio heat** | Sum of capital-at-risk across names (today's book = **3.14%**). Keep the book heat ≤ ~6–8%. | bounds a bad week |
| **Regime gate (the big one)** | Exposure capped by regime; **BEAR ⇒ near-cash**. | E4: the BEAR reversal is what sank the un-gated edge; cash there ≈ halved drawdown |
| **Concentration caps** | Sector 30 / theme 40 / cluster 20 / per-name 10 (%). | no single bet can wreck the book |
| **ATR = risk, not alpha** | Use ATR for stop distance and to **size down** the most volatile names; do not chase high-vol for return. | ATR is a volatility/risk premium (Phase J), with deeper drawdowns |
| **Hard stops, no averaging down** | Stop set at entry, never widened; losers are cut, not topped up. | removes the #1 retail blow-up |
| **Capital posture** | **Paper or ≤ 25–30% of intended capital** until ≥ 1–2 quarters of *live* forward results + a cost-aware Trust re-score justify scaling. | edge is Research-Only / not OOS-proven |

The first six layers exist in code today. The seventh is a discipline, and it is the honest price
of deploying a system whose edge is real in-sample but unproven out-of-sample.

---

## 9. Example using the current screen output (real, today)

Live `python run.py --portfolio`, 2026-06-14 — **regime NEUTRAL ⇒ 50% deployed, 50% cash**:

| # | Buy | Sector | RS | Conviction | Stop dist | Capital@risk |
|---|-----|--------|----|-----------:|----------:|-------------:|
| 1 | **HSCL** | Chemicals | MARKET_LEADER | 96 | 9.6% | 0.96% |
| 2 | **BHEL** | Capital Goods | MARKET_LEADER | 95 | 3.9% | 0.39% |
| 3 | **WOCKPHARMA** | Healthcare | MARKET_LEADER | 94 | 7.7% | 0.77% |
| 4 | **ASTERDM** | Healthcare | SECTOR_LEADER | 91 | 6.1% | 0.61% |
| 5 | **ADANIENSOL** | Power | MARKET_LEADER | 90 | 4.2% | 0.41% |

Translated to **₹5,00,000** by the §10 presenter (each name = 10% of capital; regime caps deployment
at 50% ⇒ ₹2.5L invested, ₹2.5L cash; total book heat ≈ ₹15,700 = 3.14%):

```
╔══════════════════════════════════════════════════════════════╗
║  TODAY'S PORTFOLIO — 2026-06-14    Regime: NEUTRAL            ║
╠══════════════════════════════════════════════════════════════╣
║  BUY (₹2,50,000 of ₹5,00,000):                               ║
║    1. HSCL          ₹50,000   stop −9.6%                      ║
║    2. BHEL          ₹50,000   stop −3.9%                      ║
║    3. WOCKPHARMA    ₹50,000   stop −7.7%                      ║
║    4. ASTERDM       ₹50,000   stop −6.1%                      ║
║    5. ADANIENSOL    ₹50,000   stop −4.2%                      ║
║                                                              ║
║  HOLD CASH:   ₹2,50,000 (50%)  — NEUTRAL regime, half-in     ║
║  IGNORE everything else on the screen.                       ║
║  Max positions: 5    Book risk: 3.14%                        ║
║  Next review: Friday close                                   ║
║  Expected holding: 4–12 weeks                                ║
╚══════════════════════════════════════════════════════════════╝
```

Two things this makes concrete: (1) the engine *already* produced the exact 5-name book with sizes
and stops — the only missing piece is the rupee card; (2) the **regime gate is already protecting
you** — it put you 50% in cash today without a single judgment call. In a BULL week the same card
would deploy ~100% across up to 8 names; in a STRONG_BEAR week it would say *hold cash*.

_(Note: today 4 of 5 are WATCHLIST and 1 is ACTION_NOW — i.e. the book does not depend on the
ACTION_NOW label, consistent with treating ACTION_NOW as non-load-bearing.)_

---

## 10. Exact implementation roadmap

Ordered by ROI; each step is small and independently shippable. **Steps 1–2 are pure
presentation/config (no scoring change — safe now). Step 3 is a scoring change and must pass its
pre-registered OOS A/B first.**

### Step 1 — "Today's Orders" presenter  *(no scoring change · ~half a day)*
- New `--orders --capital 500000` (or extend `--portfolio`): take the existing `PortfolioSnapshot`,
  multiply allocations by capital → **rupee amounts**, render the order card in §9 (BUY list, cash,
  "ignore everything else," book risk, next review date, holding horizon).
- Pure read of `snap.positions[*].allocation/entry/stop`; **zero** changes to scoring/sizing.
- Deliverable: a `deploy/order_card.py` renderer + CLI flag. Acceptance: card matches the live
  `--portfolio` numbers for ₹5L.

### Step 2 — Deployment policy config + two rules  *(no scoring change · ~half a day)*
- Add `config/deployment.yaml`: `capital`, `target_positions: 5`, `max_new_per_week: 3`,
  `time_stop_weeks: 12`, `posture: paper|small|full`, explicit `regime_exposure` (defaults =
  current `REGIME_EXPOSURE`).
- Implement the **time-stop** and optional **profit-trail** in `ExitAnalyzer`/`_is_reduce`
  (rule additions, not scoring). Surface a one-line **regime banner** in the order card
  ("BEAR — hold cash").
- Acceptance: a flat 12-week-old position shows EXIT; BEAR regime renders a cash card.

### Step 3 — Reconcile eligibility/conviction with the evidence  *(SCORING CHANGE · gated by OOS A/B)*
- Change candidate **eligibility** from `{ACTION_NOW, WATCHLIST}` → **RS-leader-based**
  (MARKET_LEADER/SECTOR_LEADER in a leading sector, liquidity floor), and **re-anchor conviction**
  on RS + Sector (demote actionability/RR/grade to tie-breakers); drop Breakout/Liquidity/Trend/
  Freshness influence on selection.
- This is exactly Repair-Lab **E1/E2/E6/E7**. Per the standing rule, **do not adopt it until** the
  pre-registered A/B passes on the frozen 2025–26 holdout (spread ≥ current AND OOS-stable).
  Until then, Steps 1–2 run on the *current* engine selection — already RS-leader-dominated in
  practice (see §9).

### Step 4 — Transaction-cost / turnover model  *(measurement · unblocks scale-up)*
- Add brokerage + STT + slippage to the weekly-rebalance sim (Repair-Lab blocker #5). Re-run the
  E4 gated curve **net of costs**. Gate for scaling capital: edge survives costs.

### Step 5 — Trust re-score + scale-up decision  *(measurement)*
- Re-run `analytics/trust_audit.py` on the gated + de-diluted + cost-aware model. Scale capital
  posture (paper → small → full) **only** as the OOS edge CI moves off zero and the cost-aware
  drawdown stays acceptable.

**Bottom line:** Steps 1–2 give you the < 10-minute, zero-fatigue weekly workflow on **real,
current** engine output **this week**, with no logic change and no validation debt. Steps 3–5 are
how the *capital dial* is earned — not how the *workflow* ships.

---

_Design/policy doc. No scoring, regime, ranking or factor math is changed by this file. Steps 1–2 are
presentation/config only; Step 3 is explicitly gated behind the project's standing rule that
out-of-sample validation precedes any scoring modification._
