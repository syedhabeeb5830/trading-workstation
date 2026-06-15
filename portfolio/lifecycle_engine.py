"""
portfolio/lifecycle_engine.py — Portfolio Lifecycle & Risk Management (Phase 10B)
=================================================================================
Portfolio construction is a one-time decision; portfolio MANAGEMENT is continuous.
Every time a screen runs, this engine re-evaluates the stocks you already hold and
assigns each one a clear action:

    HOLD · ADD · REDUCE · EXIT · ROTATE

It folds in three things that were otherwise separate small features:
  • Theme diversification (config/theme_map.yaml, max_theme_exposure 40%) — sector
    diversification isn't enough; Capital Goods + Defense + Power Equipment are one bet.
  • Trader action labels (entry_context → BUY_NOW / BUY_PULLBACK / WAIT_BREAKOUT /
    WAIT_PULLBACK / AVOID) — presentation only, no scoring change.
  • Lifecycle management — rule-based exits, opportunity-cost ROTATE, regime-aware ADD.

Consumes Phase-5 actionability + Phase-6 regime (+ RS/composite). Rebuilds nothing.
All analyzers are independent; LifecycleManager orchestrates.

Classes: PositionMonitor · ExitAnalyzer · AddAnalyzer · RotationAnalyzer ·
ThemeDiversifier · LifecycleManager · LifecycleSnapshot.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from scanner.sector_engine import week_id_for
from portfolio.portfolio_engine import PositionSizer, REGIME_EXPOSURE

_log = logging.getLogger(__name__)

MAX_THEME_EXPOSURE = 40.0
ROTATION_THRESHOLD = 15.0        # candidate conviction must beat held by this to ROTATE
EXTENSION_REDUCE_PCT = 15.0      # > this % stretched → REDUCE (20% in STRONG_BULL)
_THEME_PATH = Path("config/theme_map.yaml")
_HISTORY_DIR = Path("Journal/lifecycle_history")
_REPORTS_DIR = Path("reports")
_PORTFOLIO_HISTORY = Path("Journal/portfolio_history")

# entry_context → trader action (classification overrides for EXTENDED/AVOID)
_ACTION_BY_CONTEXT = {
    "BREAKOUT_SETUP": "BUY_NOW", "TREND_CONTINUATION": "BUY_NOW",
    "PULLBACK_SETUP": "BUY_PULLBACK", "BASE_BUILDING": "WAIT_BREAKOUT",
    "NO_SETUP": "AVOID",
}


def action_label(entry_context: str, classification: str) -> str:
    if classification == "EXTENDED":
        return "WAIT_PULLBACK"
    if classification == "AVOID":
        return "AVOID"
    return _ACTION_BY_CONTEXT.get(entry_context, "WAIT")


# ─────────────────────────────────────────────────────────────────────────────
# THEME MAP LOADER (PyYAML or minimal list parser, + built-in default)
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULT_THEME_MAP = {
    "INDUSTRIAL_COMPLEX": ["Capital Goods", "Construction", "Construction Materials"],
    "POWER_COMPLEX": ["Power", "Oil Gas & Consumable Fuels"],
    "FINANCIAL_COMPLEX": ["Financial Services"],
    "TECH_COMPLEX": ["Information Technology", "Telecommunication",
                     "Media Entertainment & Publication"],
    "CONSUMPTION_COMPLEX": ["Fast Moving Consumer Goods", "Consumer Services",
                            "Consumer Durables", "Automobile and Auto Components",
                            "Realty", "Services"],
    "MATERIALS_COMPLEX": ["Metals & Mining", "Chemicals", "Textiles"],
    "HEALTHCARE_COMPLEX": ["Healthcare"],
}


def _parse_list_yaml(text: str) -> dict[str, list[str]]:
    data: dict[str, list[str]] = {}
    cur = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")) and line.rstrip().endswith(":"):
            cur = line.strip()[:-1].strip()
            data[cur] = []
        elif line.strip().startswith("- ") and cur is not None:
            data[cur].append(line.strip()[2:].strip())
    return data


def _load_theme_map(path: Path = _THEME_PATH) -> dict[str, list[str]]:
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
            try:
                import yaml  # type: ignore
                data = yaml.safe_load(text)
            except ImportError:
                data = _parse_list_yaml(text)
            if isinstance(data, dict) and data:
                clean = {k: [str(x) for x in v] for k, v in data.items()
                         if isinstance(v, list) and v}
                if clean:
                    return clean
        except Exception as exc:
            _log.warning("theme_map load failed: %s — using defaults.", exc)
    return {k: list(v) for k, v in _DEFAULT_THEME_MAP.items()}


# ─────────────────────────────────────────────────────────────────────────────
# DATA TYPES
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Holding:
    ticker:      str               # .NS
    symbol:      str
    sector:      str
    allocation:  float             # % of capital
    entry_price: float
    stop_price:  Optional[float] = None
    prior_conviction: Optional[float] = None


@dataclass
class PositionState:
    ticker:         str
    symbol:         str
    sector:         str
    theme:          str
    allocation:     float
    entry_price:    float
    stop_price:     Optional[float]
    current_price:  float
    pnl_pct:        float
    rs_status:      str
    rs_bucket:      str
    grade:          str
    composite_score: float
    actionability_score: float
    rr:             float
    classification: str
    entry_context:  str
    action:         str
    conviction:     float
    extension_pct:  float
    in_universe:    bool
    stop_hit:       bool
    trend_break:    bool


@dataclass
class RotationCandidate:
    ticker:     str
    symbol:     str
    conviction: float
    theme:      str
    rs_status:  str


# ─────────────────────────────────────────────────────────────────────────────
# THEME DIVERSIFIER
# ─────────────────────────────────────────────────────────────────────────────
class ThemeDiversifier:
    def __init__(self, path: Path = _THEME_PATH, max_theme: float = MAX_THEME_EXPOSURE):
        self.map = _load_theme_map(path)
        self.sector_to_theme = {s: th for th, secs in self.map.items() for s in secs}
        self.max_theme = max_theme

    def theme_of(self, sector: str) -> str:
        return self.sector_to_theme.get(sector, sector or "DIVERSIFIED")

    def exposure(self, holdings: list[Holding]) -> dict[str, float]:
        out: dict[str, float] = {}
        for h in holdings:
            th = self.theme_of(h.sector)
            out[th] = round(out.get(th, 0.0) + h.allocation, 2)
        return out

    def room(self, theme: str, holdings: list[Holding]) -> float:
        return self.max_theme - self.exposure(holdings).get(theme, 0.0)

    def over_cap(self, holdings: list[Holding]) -> dict[str, float]:
        return {th: e for th, e in self.exposure(holdings).items() if e > self.max_theme + 1e-6}


# ─────────────────────────────────────────────────────────────────────────────
# POSITION MONITOR
# ─────────────────────────────────────────────────────────────────────────────
class PositionMonitor:
    def __init__(self):
        self.sizer = PositionSizer()

    def assess(self, h: Holding, screen, theme: ThemeDiversifier) -> PositionState:
        act = screen.act_snap.get(h.ticker)
        comp = screen.comp_snap.get(h.ticker)
        rs = screen.rs_snap.get(h.ticker)
        df = screen.feed.data.get(h.ticker)
        in_universe = act is not None and df is not None and not df.empty

        current = float(df["Close"].iloc[-1]) if (df is not None and not df.empty) else h.entry_price
        pnl = ((current - h.entry_price) / h.entry_price * 100) if h.entry_price else 0.0
        rs_status = (rs.status if rs else act.rs_status) if (rs or act) else "LAGGARD"
        rs_bucket = rs.bucket if rs else "NA"
        grade = comp.grade if comp else "D"
        act_score = act.actionability_score if act else 0.0
        rr = act.rr_ratio if act else 0.0
        ctx = act.entry_context if act else "NO_SETUP"
        ext = act.extension_pct if act else 0.0
        cls = act.classification if act else "AVOID"
        conv = self.sizer.conviction(rs_status, act_score, rr, grade) if act else 0.0
        stop_hit = bool(h.stop_price) and current <= h.stop_price
        trend_break = ctx == "NO_SETUP"

        return PositionState(
            h.ticker, h.symbol, h.sector, theme.theme_of(h.sector), h.allocation,
            h.entry_price, h.stop_price, round(current, 2), round(pnl, 2),
            rs_status, rs_bucket, grade, comp.score if comp else 0.0, act_score, rr,
            cls, ctx, action_label(ctx, cls), round(conv, 1), ext,
            in_universe, stop_hit, trend_break)


# ─────────────────────────────────────────────────────────────────────────────
# ANALYZERS (independent)
# ─────────────────────────────────────────────────────────────────────────────
class ExitAnalyzer:
    """Rule-based exits — never discretionary. Tightens with a weaker regime."""

    def check(self, s: PositionState, regime: str) -> Optional[str]:
        if s.stop_hit:
            return f"Stop hit (price {s.current_price} ≤ stop {s.stop_price})"
        if not s.in_universe:
            return "Dropped out of the screen universe"
        if s.rs_status == "LAGGARD":
            return "RS collapsed to LAGGARD"
        if s.grade == "D":
            return "Composite grade collapsed to D"
        if s.trend_break:
            return "Trend broken (no constructive structure)"
        if regime == "STRONG_BEAR" and s.rs_status not in ("MARKET_LEADER", "SECTOR_LEADER"):
            return "Strong-bear regime — exit all but elite leaders"
        if regime == "WEAK_BEAR" and (s.rs_bucket == "BOTTOM_50" or s.grade in ("C", "D")):
            return "Weak-bear regime — trim laggards to raise cash"
        return None


class AddAnalyzer:
    """Add to winners on a pullback — only when the regime and exposure allow."""

    def check(self, s: PositionState, regime: str, theme_room: float,
              exposure_room: float) -> Optional[str]:
        if regime in ("WEAK_BEAR", "STRONG_BEAR"):
            return None                                   # raise cash, don't add
        if theme_room <= 1.0 or exposure_room <= 1.0:
            return None
        strong = s.rs_status in ("MARKET_LEADER", "SECTOR_LEADER")
        if not strong:
            return None
        if s.entry_context == "PULLBACK_SETUP":
            return "Leader pulled back to support — add into strength"
        if regime == "STRONG_BULL" and s.entry_context in ("BREAKOUT_SETUP", "TREND_CONTINUATION"):
            return "Strong-bull regime — pyramid the leader"
        return None


class RotationAnalyzer:
    """Opportunity-cost exits: replace a holding with a much stronger same-theme name."""

    def check(self, s: PositionState, candidates_by_theme: dict[str, list[RotationCandidate]],
              regime: str, threshold: float = ROTATION_THRESHOLD
              ) -> Optional[tuple[RotationCandidate, str]]:
        cands = [c for c in candidates_by_theme.get(s.theme, []) if c.ticker != s.ticker]
        if not cands:
            return None
        best = max(cands, key=lambda c: c.conviction)
        delta = best.conviction - s.conviction
        if delta > threshold:
            return best, (f"{best.symbol} conviction {best.conviction:.0f} vs held "
                          f"{s.conviction:.0f} (+{delta:.0f}), same theme {s.theme}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class LifecycleRow:
    ticker:         str
    symbol:         str
    sector:         str
    theme:          str
    allocation:     float
    status:         str
    reason:         str
    action:         str
    rs_status:      str
    grade:          str
    conviction:     float
    pnl_pct:        float
    current_price:  float
    entry_context:  str
    rotate_to:      Optional[str] = None


@dataclass
class LifecycleSnapshot:
    generated_at:    str
    week_id:         str
    regime:          str
    rows:            list[LifecycleRow]
    theme_exposure:  dict
    theme_warnings:  dict
    summary:         dict

    def to_dict(self) -> dict:
        return {"generated_at": self.generated_at, "week_id": self.week_id,
                "regime": self.regime, "summary": self.summary,
                "theme_exposure": self.theme_exposure, "theme_warnings": self.theme_warnings,
                "positions": [asdict(r) for r in self.rows]}

    def export_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def to_csv(self) -> str:
        return pd.DataFrame([asdict(r) for r in self.rows]).to_csv(index=False)

    def to_markdown(self) -> str:
        L = [f"# Portfolio Review — {self.generated_at[:10]}", "",
             f"**Regime: {self.regime}** · " +
             "  ".join(f"{k}={v}" for k, v in self.summary.items()), "",
             "| Ticker | Status | Action | Reason | RS | Grade | Conv | P&L% |",
             "|--------|--------|--------|--------|----|-------|-----:|-----:|"]
        for r in self.rows:
            rot = f" → {r.rotate_to}" if r.rotate_to else ""
            L.append(f"| {r.symbol} | {r.status}{rot} | {r.action} | {r.reason} | "
                     f"{r.rs_status} | {r.grade} | {r.conviction} | {r.pnl_pct} |")
        if self.theme_warnings:
            L += ["", "## ⚠ Theme over-concentration (cap "
                  f"{MAX_THEME_EXPOSURE}%)"]
            for th, e in self.theme_warnings.items():
                L.append(f"- {th}: {e}%")
        L += ["", "## Theme exposure"]
        for th, e in sorted(self.theme_exposure.items(), key=lambda kv: -kv[1]):
            L.append(f"- {th}: {e}%")
        return "\n".join(L) + "\n"

    def save(self, history_dir: Path = _HISTORY_DIR) -> Path:
        history_dir.mkdir(parents=True, exist_ok=True)
        path = history_dir / f"{self.week_id}.json"
        path.write_text(self.export_json())
        return path


# ─────────────────────────────────────────────────────────────────────────────
# MANAGER
# ─────────────────────────────────────────────────────────────────────────────
class LifecycleManager:
    def __init__(self, max_theme: float = MAX_THEME_EXPOSURE):
        self.theme = ThemeDiversifier(max_theme=max_theme)
        self.monitor = PositionMonitor()
        self.exit = ExitAnalyzer()
        self.add = AddAnalyzer()
        self.rotation = RotationAnalyzer()
        self.sizer = PositionSizer()

    def _candidates_by_theme(self, screen, held: set[str]) -> dict[str, list[RotationCandidate]]:
        out: dict[str, list[RotationCandidate]] = {}
        for r in screen.act_snap.top_n(40):
            if r.classification not in ("ACTION_NOW", "WATCHLIST") or r.ticker in held:
                continue
            conv = self.sizer.conviction(r.rs_status, r.actionability_score, r.rr_ratio, r.grade)
            th = self.theme.theme_of(r.sector)
            out.setdefault(th, []).append(
                RotationCandidate(r.ticker, r.symbol, round(conv, 1), th, r.rs_status))
        return out

    @staticmethod
    def _is_reduce(s: PositionState, regime: str) -> Optional[str]:
        cap = 20.0 if regime == "STRONG_BULL" else EXTENSION_REDUCE_PCT
        if s.extension_pct > cap:
            return f"Extended {s.extension_pct:.0f}% above breakout/EMA20 — trim"
        if s.rs_status in ("EMERGING_LEADER", "NEUTRAL") and s.rs_bucket in ("TOP_50", "BOTTOM_50"):
            return "Leadership weakening (RS slipping out of the top tier) — trim"
        return None

    def review(self, holdings: list[Holding], screen,
               regime_override: Optional[str] = None, persist: bool = False) -> LifecycleSnapshot:
        regime = regime_override or screen.regime.regime
        held = {h.ticker for h in holdings}
        cands = self._candidates_by_theme(screen, held)
        theme_exp = self.theme.exposure(holdings)
        deployed = sum(h.allocation for h in holdings)
        exposure_room = REGIME_EXPOSURE.get(regime, 50) - deployed

        rows: list[LifecycleRow] = []
        for h in holdings:
            s = self.monitor.assess(h, screen, self.theme)
            rotate_to = None
            exit_reason = self.exit.check(s, regime)
            if exit_reason:
                status, reason = "EXIT", exit_reason
            else:
                rot = self.rotation.check(s, cands, regime)
                reduce_reason = self._is_reduce(s, regime)
                theme_room = self.theme.max_theme - theme_exp.get(s.theme, 0.0)
                add_reason = self.add.check(s, regime, theme_room, exposure_room)
                if rot:
                    status, reason, rotate_to = "ROTATE", rot[1], rot[0].symbol
                elif reduce_reason:
                    status, reason = "REDUCE", reduce_reason
                elif add_reason:
                    status, reason = "ADD", add_reason
                else:
                    status, reason = "HOLD", "RS strong, trend intact, no better replacement"
            rows.append(LifecycleRow(
                s.ticker, s.symbol, s.sector, s.theme, s.allocation, status, reason,
                s.action, s.rs_status, s.grade, s.conviction, s.pnl_pct, s.current_price,
                s.entry_context, rotate_to))

        summary: dict[str, int] = {}
        for r in rows:
            summary[r.status] = summary.get(r.status, 0) + 1
        summary = {k: summary.get(k, 0) for k in ("HOLD", "ADD", "REDUCE", "EXIT", "ROTATE")}

        snap = LifecycleSnapshot(
            datetime.now().isoformat(timespec="seconds"), week_id_for(), regime, rows,
            {k: round(v, 1) for k, v in theme_exp.items()},
            self.theme.over_cap(holdings), summary)
        if persist:
            snap.save()
        return snap


# ─────────────────────────────────────────────────────────────────────────────
# HOLDINGS LOADING
# ─────────────────────────────────────────────────────────────────────────────
def load_holdings_from_portfolio(history_dir: Path = _PORTFOLIO_HISTORY) -> list[Holding]:
    """Reconstruct holdings from the most recent constructed portfolio snapshot."""
    if not history_dir.exists():
        return []
    files = sorted(history_dir.glob("*.json"))
    if not files:
        return []
    try:
        data = json.loads(files[-1].read_text())
    except Exception:
        return []
    out = []
    for p in data.get("positions", []):
        sym = str(p.get("ticker", "")).upper()
        out.append(Holding(
            ticker=sym if sym.endswith(".NS") else f"{sym}.NS", symbol=sym,
            sector=p.get("sector", "DIVERSIFIED"), allocation=float(p.get("allocation", 0)),
            entry_price=float(p.get("entry", 0) or 0),
            stop_price=float(p["stop"]) if p.get("stop") else None,
            prior_conviction=p.get("conviction")))
    return out


def resolve_holdings(sector_map: dict[str, str]) -> tuple[list[Holding], str]:
    """Unambiguous holdings source (RC1): distinguishes a live Kite account (with
    or without positions) from the simulated fallback. Returns (holdings, source)."""
    kite = load_holdings_from_kite(sector_map)
    if kite is None:                       # no valid Kite session
        return (load_holdings_from_portfolio(),
                "SIMULATED — last constructed portfolio (no Kite session)")
    if len(kite) == 0:                     # connected, but account holds nothing
        return [], "ZERODHA KITE (live) — account holds 0 equity positions"
    return kite, "ZERODHA KITE (live, real holdings)"


def load_holdings_from_kite(sector_map: dict[str, str]) -> Optional[list[Holding]]:
    """Real broker holdings (if a Kite session exists), else None."""
    try:
        from integrations.zerodha import get_client_or_none
        kc = get_client_or_none()
        if kc is None:
            return None
        df = kc.holdings_df()
        if df is None or df.empty:
            return []
        total = float((df["quantity"] * df["avg_price"]).sum()) or 1.0
        out = []
        for _, r in df.iterrows():
            t = str(r["ticker"]).upper()
            val = float(r["quantity"]) * float(r["avg_price"])
            out.append(Holding(
                ticker=t, symbol=t[:-3] if t.endswith(".NS") else t,
                sector=sector_map.get(t, "DIVERSIFIED"),
                allocation=round(val / total * 100, 2),
                entry_price=float(r["avg_price"]), stop_price=None))
        return out
    except Exception as exc:
        _log.warning("Kite holdings unavailable: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# RENDER + EXPORT + RUN
# ─────────────────────────────────────────────────────────────────────────────
_STATUS_COLOR = {"HOLD": "\033[2m", "ADD": "\033[92m", "REDUCE": "\033[93m",
                 "EXIT": "\033[91m", "ROTATE": "\033[95m"}


def render_review(snap: LifecycleSnapshot) -> None:
    G, R, Y, B, D, RST = ("\033[92m", "\033[91m", "\033[93m", "\033[1m", "\033[2m", "\033[0m")
    print(f"\n  {B}{'═'*66}{RST}")
    print(f"  {B}  PORTFOLIO REVIEW{RST}   regime {B}{snap.regime}{RST}")
    print(f"  {B}{'═'*66}{RST}")
    print(f"  {D}" + "   ".join(f"{k} {v}" for k, v in snap.summary.items()) + RST)
    if not snap.rows:
        print(f"\n  {Y}No holdings found. Build a portfolio first (python run.py --portfolio) "
              f"or log in to Kite.{RST}\n")
        return
    print()
    for r in snap.rows:
        col = _STATUS_COLOR.get(r.status, "")
        rot = f" → {r.rotate_to}" if r.rotate_to else ""
        print(f"  {B}{r.symbol:12s}{RST}  {col}{r.status:6s}{rot}{RST}  "
              f"{D}[{r.action}]{RST}  {r.rs_status[:14]:14s}  {D}{r.theme}{RST}")
        print(f"      {D}{r.reason}  ·  conv {r.conviction}  ·  P&L {r.pnl_pct:+.1f}%{RST}")

    if snap.theme_warnings:
        print(f"\n  {R}⚠ THEME OVER-CONCENTRATION (cap {MAX_THEME_EXPOSURE:.0f}%){RST}")
        for th, e in snap.theme_warnings.items():
            print(f"    {R}{th}: {e}%{RST}")
    print(f"\n  {B}Theme exposure{RST}")
    print("  " + "   ".join(f"{th} {e:.0f}%" for th, e in
                            sorted(snap.theme_exposure.items(), key=lambda kv: -kv[1])))
    print()


def export_review(snap: LifecycleSnapshot, reports_dir: Path = _REPORTS_DIR) -> dict:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    paths = {"json": reports_dir / f"review_{stamp}.json",
             "csv": reports_dir / f"review_{stamp}.csv",
             "md": reports_dir / f"review_{stamp}.md"}
    paths["json"].write_text(snap.export_json(), encoding="utf-8")
    paths["csv"].write_text(snap.to_csv(), encoding="utf-8")
    paths["md"].write_text(snap.to_markdown(), encoding="utf-8")
    return paths


def run_review_portfolio(force_refresh: bool = False) -> int:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    from screen.screen_runner import (build_screen, BenchmarkUnavailableError,
                                       render_benchmark_abort)
    print("\n  Reviewing portfolio (screen → assess holdings → lifecycle status)...")
    try:
        res = build_screen(force_refresh=force_refresh, persist=True)
    except BenchmarkUnavailableError as exc:
        render_benchmark_abort(exc)
        return 2
    holdings, source = resolve_holdings(res.universe.sector_map)
    print(f"  Holdings source: {source} ({len(holdings)} positions)")
    snap = LifecycleManager().review(holdings, res, persist=True)
    render_review(snap)
    paths = export_review(snap)
    print(f"  Exports: {paths['json']} · {paths['csv']} · {paths['md']}\n")
    return 0
