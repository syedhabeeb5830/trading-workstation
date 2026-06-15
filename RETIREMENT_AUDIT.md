# `--today` Legacy Engine — Retirement Audit (RC1)

> **Date:** 2026-06-12 · **Scope:** outcome value only (not architecture).
> **Verdict: C — DEMOTE.** Use `--today` for **research/debug only**. Route all stock
> discovery through `--screen`. No deletion yet; no scoring/weights/factors/logic changed.
> Read alongside `HANDOFF.md`.

---

## Decision & operational routing

| Function | Use | Do **not** use |
|---|---|---|
| Stock discovery | `--screen` | ~~`--today`~~ |
| Position sizing / construction | `--portfolio` | — |
| Lifecycle / holdings review | `--review-portfolio` | — |
| Execution & position tooling | `--place`, `--positions`, `--reconcile`, `--trail`, `--partial`, `--gtts`, `--kite-login`, `--doctor` | — |
| Legacy discovery (`--today` setup-score) | **research/debug only** | **not for trade decisions** |

`--today` is **demoted, not deleted** — its execution/position-management commands are
retained until equivalent functionality exists on the new path. The cleanup that
accompanies this audit is **display + documentation only** (a deprecation banner on the
`--today` cockpit; help-text and HANDOFF notes). No model behaviour was modified.

---

## Data-availability caveat (read first)

RC1 has only just begun accumulating live snapshots (HANDOFF §10):

- **`--screen` persisted snapshots: 1 week** (`2026-W24`). No multi-day screen history.
- **`--today` scan log: ~3 weeks** (`2026-05-23 → 2026-06-12`).
- **OHLCV cache: 5y daily**, full universe — forward returns computable only where future
  data exists (so **5D/10D measurable from late-May; 20D is not** in this window).
- **Rolling validation: 103 screens / 11,556 obs** — the authoritative long-horizon factor
  evidence (`reports/rolling_report_2026-06-11.md`).

Therefore the forward-return sections below are **corroborative (2 weeks, one mild
down-drift regime, small n)**, while the coverage section is **deterministic**. The verdict
is weighted accordingly.

---

## 1. Coverage Audit — *deterministic* (screen 2026-06-12 vs 101-name watchlist)

| Screen rank band | In watchlist | Coverage |
|---|---|---|
| **Top 5** | 0 / 5 | **0%** |
| **Top 10** | 0 / 10 | **0%** |
| **Top 20** | 3 / 20 (SYRMA, THERMAX, CGPOWER) | **15%** |
| **All MARKET_LEADERs** | 6 / 48 | **12.5%** |

Most-missed leaders: DATAPATTNS, RRKABEL, DEEPAKFERT, LLOYDSME, JINDALSAW, ADANIPOWER,
BHEL, KIRLOSENG, HFCL, WOCKPHARMA. **42 of 48 validated market leaders are structurally
invisible to `--today`.** The only proven edge (RS leadership) lives almost entirely
outside the watchlist's reach.

## 2. Alpha Contribution Audit — *measured* (late-May; benchmark = equal-weight 500-name universe)

| Base | Horizon | Universe (EW) | `--today` READY | `--today` WATCH | Screen LEADER¹ | Screen LAGGARD |
|---|---|--:|--:|--:|--:|--:|
| 05-26 | 5D | −0.83% | **−1.73%** (n11) | −0.07% (n37) | +4.07% | −2.44% |
| 05-26 | 10D | −1.02% | **−1.60%** (n11) | −1.71% (n37) | +6.61% | −4.07% |
| 05-27 | 5D | −1.58% | **−2.75%** (n16) | −1.58% (n37) | +2.46% | −3.07% |
| 05-27 | 10D | −2.62% | **−3.43%** (n16) | −2.57% (n37) | +2.12% | −4.98% |

¹ Leader group uses current (W24) leadership → mild lookahead; directional only, consistent
with the no-lookahead rolling evidence.

**No measurable alpha; mild negative selection.** `--today`'s highest-conviction READY tier
underperformed the equal-weight universe in every window and underperformed its own WATCH
tier (conviction is *inverted* vs forward return).

## 3. Contradiction Audit — *measured* (realized to 06-12)

| Ticker | `--today` | `--screen` | 5D | 10D | →06-12 | Correct engine |
|---|---|---|--:|--:|--:|---|
| KPITTECH | WATCH | AVOID/LAGGARD #461 | +3.13% | −3.64% | **−6.59%** | **`--screen`** (clear) |
| ASIANPAINT | **READY (#1)** | AVOID C #229 | +0.52% | +2.31% | +2.57% | `--today` (mild) |
| SYRMA | WATCH | EXTENDED A #12 | +13.4% | +16.3% | +16.3% | timing→`--today`; **discovery→`--screen`** |
| CGPOWER | WATCH | WATCHLIST A #20 | +3.25% | +3.65% | +3.29% | both |
| CUMMINSIND | WATCH | WATCHLIST B #37 | +5.55% | +4.25% | +4.23% | both |
| THERMAX | WATCH | WATCHLIST A #15 | +12.0% | +6.86% | +5.93% | both |

**No systematic `--today` advantage.** The screen was decisively right on the genuine
conflict (KPITTECH, −6.6%). `--today`'s one real "win" (SYRMA, +16%) was a name the screen
**already ranked #12 as an A-grade MARKET_LEADER** — the discovery was the screen's;
`--today` differed only on entry timing.

## 4. Missed-Leader Audit — *opportunity cost*

The Top-10 screen leaders absent from the watchlist are exactly the group that returned
**+4.07% (5D) / +6.61% (10D)** from 05-26 while `--today`'s reachable picks returned −1.7%
and the universe −1.0%. Quantified opportunity cost of watchlist exclusion in-window:
**~+6 to +8 pts of 10-day return foregone**, concentrated in names `--today` can never see.

## 5. Incremental Information Audit — *limited by data*

A clean controlled test (do coil/setup/breakout add predictive power after controlling for
RS leadership / composite / actionability) **requires multi-date screen history that does not
yet exist (1 week)** and cannot be run today without re-deriving past screens. Available
evidence:

- Rolling study (103 screens): the **only** significant predictor is RS leadership
  (10D t=2.55, 20D t=2.65) — which `--today` weights as secondary and largely cannot access.
- In-window: `--today`'s actionable flag carried no positive forward-return signal and its
  conviction tier was return-inverted.

**Conclusion: no incremental predictive power demonstrated; the proven predictor lives
entirely in `--screen`.** (Stated as a limitation, not an over-claimed regression.)

---

## Verdict

### C — DEMOTE
- 0% coverage of validated Top-10 leadership; 12.5% of all leaders (§1, deterministic).
- No measurable alpha; mild negative selection (§2).
- Loses the one real contradiction (KPITTECH); its sole win was a screen-discovered leader (§3).
- No forward-return validation of its signals; proven edge is in `--screen` (§5).

**Not A/B:** no independent alpha exists, and the only edge-bearing factor (RS) already lives
in `--screen` — nothing demonstrated worth porting. **Not yet D (full retire):** the
forward-return sample is thin (2 weeks, one regime, n=11–16), and execution tooling is still
entangled. Hard-deleting now would itself be overfitting.

### Key question — *would performance change if `--today` disappeared tomorrow?*
**Stock selection: unchanged → slightly improved.** No measurable alpha is lost; removing it
eliminates the demonstrated misdirection risk (steered toward weak-RS names like KPITTECH,
away from leaders like DATAPATTNS). **Only the execution/position commands would be missed —
those are separable and must be preserved or ported.**

### Graduation criteria → D (RETIRE the discovery path)
Retire outright once **≥8–12 weeks of accumulated `--screen` snapshots** confirm,
out-of-sample: (a) the leader-coverage gap persists, and (b) no incremental alpha from
`--today`'s signals after controlling for RS/composite/actionability. Until then: demoted.

---

## Reproduce

```
python run.py --screen                  # current validated board (Top 20 / Top 5)
# coverage:    screen Top-N ∩ scanner/watchlist.py WATCHLIST
# returns:     cache/ohlcv/<date>_5y.csv  vs  Journal/scan_log.csv
# factor edge: reports/rolling_report_2026-06-11.md  (103 screens, 11,556 obs)
```

_What this audit changed in the repo: a deprecation banner on the `--today` cockpit, help
text, and HANDOFF notes — display/documentation only. Scoring, weights, factors, regime,
composite, RS, portfolio, and lifecycle logic are untouched (RC1 freeze intact)._
