"""
verification/phase6.py — Phase 6 (Market Regime Engine) acceptance checks
=========================================================================
Proves the Phase-6 contract (must pass 8/8) and prints the sample
RegimeSnapshot + screen regime block.

  1. Regime score exists and is bounded 0..100.
  2. All five component scores exist.
  3. Classification assigned (valid label).
  4. Deterministic across repeated runs.
  5. History persisted (Journal/regime_history JSON + CSV).
  6. Volatility override works (synthetic extreme ATR ⇒ VOLATILE).
  7. Breadth reacts correctly (80% > EMA50 scores higher than 20%).
  8. CLI screen renders the regime block.

Usage:
    python verification/phase6.py
"""

from __future__ import annotations

import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from screen.screen_runner import build_screen, render_cockpit                  # noqa: E402
from scanner.regime_engine import (                                            # noqa: E402
    RegimeClassifier, BreadthAnalyzer, VolatilityAnalyzer, _HISTORY_DIR, _HISTORY_CSV)

logging.basicConfig(level=logging.WARNING, format="  [%(levelname)s] %(message)s")
GREEN, RED, DIM, BOLD, RST = "\033[92m", "\033[91m", "\033[2m", "\033[1m", "\033[0m"
VALID = {"STRONG_BULL", "BULL", "NEUTRAL", "RANGE", "WEAK_BEAR", "STRONG_BEAR", "VOLATILE"}


def _check(label: str, ok: bool, detail: str = "") -> bool:
    mark = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
    print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))
    return ok


def _trend_df(start: float, end: float, bars: int = 220) -> pd.DataFrame:
    idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=bars, freq="B")
    c = pd.Series(np.linspace(start, end, bars), index=idx)
    return pd.DataFrame({"Open": c, "High": c * 1.005, "Low": c * 0.995,
                         "Close": c, "Volume": [1_000_000] * bars}, index=idx)


def _extreme_vol_df(bars: int = 220) -> pd.DataFrame:
    idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=bars, freq="B")
    base = 20000 + np.cumsum(np.random.RandomState(1).randn(bars) * 50)
    c = pd.Series(base, index=idx)
    return pd.DataFrame({"Open": c, "High": c * 1.06, "Low": c * 0.94,
                         "Close": c, "Volume": [1_000_000] * bars}, index=idx)


def main() -> int:
    print(f"\n  {'='*64}\n  PHASE 6 VERIFICATION — Market Regime Engine\n  {'='*64}")
    results: list[bool] = []

    res = build_screen(period="1y", persist=True)
    reg = res.regime
    render_cockpit(res)   # sample screen output (includes the regime block)

    print(f"  {BOLD}══ SAMPLE RegimeSnapshot ══{RST}")
    import json
    print(json.dumps({k: v for k, v in reg.to_dict().items() if k != "detail"}, indent=2))

    print(f"\n  {'─'*64}\n  CHECKS\n  {'─'*64}")

    # ── 1. Score exists & bounded ────────────────────────────────────────────
    results.append(_check("Regime score exists & bounded 0..100",
                          isinstance(reg.regime_score, (int, float))
                          and 0 <= reg.regime_score <= 100,
                          f"score={reg.regime_score}"))

    # ── 2. All component scores exist ────────────────────────────────────────
    comps = [reg.trend_score, reg.breadth_score, reg.leadership_score,
             reg.participation_score, reg.volatility_score]
    results.append(_check("All five component scores exist",
                          all(isinstance(c, (int, float)) for c in comps),
                          f"trend={reg.trend_score} breadth={reg.breadth_score} "
                          f"lead={reg.leadership_score} part={reg.participation_score} "
                          f"vol={reg.volatility_score}"))

    # ── 3. Classification assigned ───────────────────────────────────────────
    results.append(_check("Classification assigned (valid label)",
                          reg.regime in VALID, f"regime={reg.regime}"))

    # ── 4. Deterministic (rebuild from the same snapshots/data) ──────────────
    from scanner.relative_strength_engine import fetch_nifty
    nifty = fetch_nifty(period="1y")
    index_dfs = {"Nifty50": nifty}
    clf = RegimeClassifier()
    kw = dict(ohlcv=res.feed.data, index_dfs=index_dfs, sector_snapshot=res.sector_snap,
              rs_snapshot=res.rs_snap, composite_snapshot=res.comp_snap,
              actionability_snapshot=res.act_snap, vix_df=None)
    r1 = clf.analyze(**kw)
    r2 = clf.analyze(**kw)
    results.append(_check("Deterministic across repeated runs",
                          (r1.regime, r1.regime_score) == (r2.regime, r2.regime_score),
                          f"{r1.regime} {r1.regime_score}"))

    # ── 5. History persisted ─────────────────────────────────────────────────
    jp = _HISTORY_DIR / f"{reg.week_id}.json"
    results.append(_check("History persisted (JSON + CSV)",
                          jp.exists() and _HISTORY_CSV.exists(), f"{jp}"))

    # ── 6. Volatility override (synthetic extreme) ───────────────────────────
    extreme = _extreme_vol_df()
    v = VolatilityAnalyzer().analyze(extreme)
    r_ext = clf.analyze(ohlcv=res.feed.data, index_dfs={"Nifty50": extreme},
                        sector_snapshot=res.sector_snap, rs_snapshot=res.rs_snap,
                        composite_snapshot=res.comp_snap,
                        actionability_snapshot=res.act_snap, vix_df=None)
    results.append(_check("Volatility override ⇒ VOLATILE",
                          v["state"] == "VOLATILE" and r_ext.regime == "VOLATILE",
                          f"vol_state={v['state']} (ATR {v['atr_pct']}%), regime={r_ext.regime}"))

    # ── 7. Breadth reacts (80% vs 20% above EMA50) ───────────────────────────
    up = _trend_df(100, 200)      # price well above EMAs
    dn = _trend_df(200, 100)      # price well below EMAs
    strong = {f"U{i}.NS": up for i in range(8)} | {f"D{i}.NS": dn for i in range(2)}
    weak = {f"U{i}.NS": up for i in range(2)} | {f"D{i}.NS": dn for i in range(8)}
    bs = BreadthAnalyzer().analyze(strong)
    bw = BreadthAnalyzer().analyze(weak)
    results.append(_check("Breadth reacts correctly (80% > 20%)",
                          bs["score"] > bw["score"]
                          and bs["metrics"]["above_ema50"] > bw["metrics"]["above_ema50"],
                          f"strong={bs['metrics']['above_ema50']:.0f}%→{bs['score']:.0f}  "
                          f"weak={bw['metrics']['above_ema50']:.0f}%→{bw['score']:.0f}"))

    # ── 8. CLI renders regime block ──────────────────────────────────────────
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        render_cockpit(res)
    out = buf.getvalue()
    results.append(_check("CLI screen renders regime block",
                          "MARKET REGIME" in out and reg.regime in out
                          and "Breadth" in out,
                          "regime header + scores present"))

    print(f"\n  {'='*64}")
    passed = sum(results)
    ok = passed == len(results)
    print(f"  {(GREEN if ok else RED)}{passed}/{len(results)} checks passed{RST}")
    print(f"  {'='*64}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
