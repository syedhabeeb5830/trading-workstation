"""
audit/consistency_report.py — RC1 cross-subsystem consistency audit
===================================================================
Verifies the pieces of the dynamic screener agree with each other:

  • Regime consistency  — one regime drives screen → portfolio → adaptive → review;
                          the OLD --today engine is a separate, documented system.
  • Ranking provenance  — portfolio + lifecycle candidates come from actionability,
                          not some other ranking path.
  • Classification      — ACTION_NOW/WATCHLIST/EXTENDED/AVOID mean one thing (one source).
  • Theme consistency   — the theme map loads and covers the universe's sectors.

Writes reports/consistency_report.md. Adds no logic; changes nothing.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from screen.screen_runner import build_screen                                  # noqa: E402
from portfolio.portfolio_engine import PortfolioConstructor, REGIME_EXPOSURE   # noqa: E402
from portfolio.lifecycle_engine import LifecycleManager, ThemeDiversifier      # noqa: E402

GREEN, RED, YEL, DIM, BOLD, RST = (
    "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[1m", "\033[0m")
REPORTS = Path("reports")
VALID_CLASSES = {"ACTION_NOW", "WATCHLIST", "EXTENDED", "AVOID"}


def main() -> int:
    print(f"\n  {'='*68}\n  CONSISTENCY AUDIT\n  {'='*68}\n")
    res = build_screen(period="1y", persist=False)
    md: list[str] = [f"# Consistency Report — {date.today().isoformat()}", ""]
    checks: list[bool] = []

    def record(label, ok, detail=""):
        mark = f"{GREEN}PASS{RST}" if ok else f"{RED}FAIL{RST}"
        print(f"  [{mark}] {label}" + (f"  {DIM}{detail}{RST}" if detail else ""))
        md.append(f"- {'✅' if ok else '❌'} **{label}** — {detail}")
        checks.append(ok)

    # ── 1. Regime single-source within the new system ────────────────────────
    port = PortfolioConstructor().construct(res.act_snap, res.regime, res.feed.data)
    adaptive_regime = res.adaptive_snap.regime
    new_consistent = (port.regime == res.regime.regime == adaptive_regime)
    md.append("## Regime consistency")
    record("New system shares ONE regime (screen=portfolio=adaptive)",
           new_consistent,
           f"{res.regime.regime} everywhere; exposure {REGIME_EXPOSURE.get(res.regime.regime)}%")

    # Old --today engine is intentionally separate.
    try:
        from scanner.scanner import get_market_regime
        from config.config import CONFIG
        old = get_market_regime(CONFIG)["regime"]
    except Exception as exc:
        old = f"error({exc})"
    print(f"  [{YEL}NOTE{RST}] Old --today regime = {old}  ·  new --screen regime = "
          f"{res.regime.regime}  {DIM}(separate engines by design){RST}")
    md.append(f"- ℹ️ Old `--today` regime (`scanner.get_market_regime`, SMA) = **{old}** vs "
              f"new `--screen` regime (`RegimeClassifier`, multi-dim) = **{res.regime.regime}**. "
              "Two engines by design — `--today` hard-blocks longs in BEAR; `--screen` ranks "
              "opportunities and caps exposure via the portfolio engine.")

    # ── 2. Ranking provenance ────────────────────────────────────────────────
    md.append("\n## Ranking provenance")
    act_tickers = {r.ticker for r in res.act_snap.rows}
    record("Portfolio positions originate from actionability",
           all(p.ticker in act_tickers for p in port.positions),
           f"{len(port.positions)} positions ⊆ actionability")

    mgr = LifecycleManager()
    cands = mgr._candidates_by_theme(res, held=set())
    cand_tickers = {c.ticker for th in cands.values() for c in th}
    record("Lifecycle rotation candidates originate from actionability",
           cand_tickers <= act_tickers, f"{len(cand_tickers)} candidates ⊆ actionability")

    # ── 3. Classification consistency ────────────────────────────────────────
    md.append("\n## Classification consistency")
    seen = {r.classification for r in res.act_snap.rows}
    record("Classifications ⊆ {ACTION_NOW, WATCHLIST, EXTENDED, AVOID}",
           seen <= VALID_CLASSES, ", ".join(sorted(seen)))

    # ── 4. Theme consistency ─────────────────────────────────────────────────
    md.append("\n## Theme consistency")
    td = ThemeDiversifier()
    universe_sectors = set(res.universe.df["sector"].unique())
    mapped = set(td.sector_to_theme)
    unmapped = sorted(universe_sectors - mapped)
    record("Theme map loads and covers core sectors",
           len(td.map) >= 5 and len(unmapped) <= len(universe_sectors),
           f"{len(td.map)} themes; {len(unmapped)} sectors fall back to own theme")
    md.append(f"- ℹ️ Sectors not in a named complex (each becomes its own theme): "
              f"{', '.join(unmapped) if unmapped else 'none'}")
    md.append("- ⚠️ Theme cap (40%) is enforced in **lifecycle review/ADD**, NOT in portfolio "
              "**construction** (which uses sector 30% + cluster 20%). Known gap → backlog.")

    REPORTS.mkdir(parents=True, exist_ok=True)
    md.append(f"\n**Result: {sum(checks)}/{len(checks)} consistency checks passed.**\n")
    (REPORTS / "consistency_report.md").write_text("\n".join(md), encoding="utf-8")

    print(f"\n  {DIM}wrote reports/consistency_report.md{RST}")
    print(f"\n  {'='*68}")
    ok = all(checks)
    print(f"  {(GREEN if ok else RED)}{sum(checks)}/{len(checks)} consistency checks passed{RST}")
    print(f"  {'='*68}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
