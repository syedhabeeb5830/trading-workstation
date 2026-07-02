"""
portfolio/portfolio_engine.py — Portfolio Construction & Capital Allocation (Phase 9)
=====================================================================================
Turns the screener from a discovery system ("what should I buy?") into a portfolio
decision system ("how much, how many, which to skip?").

Capital is allocated by CONVICTION × RISK × REGIME — not equal weight, not
"highest score wins". Per the Phase-8 evidence, conviction is anchored on the only
statistically proven edge first:

    conviction = 0.55 · Relative Strength      (PROVEN alpha — primary)
               + 0.45 · Actionability          (entry quality / tradability)
               ± grade modifier (≤5 pts)        (secondary only — fragile signal)

Risk/Reward carries ZERO weight (2026-07-02 consistency fix): analytics/
rr_validation proved geometric RR non-predictive OOS — it previously held 20%
here, which made the plan's conviction ranking silently disagree with the
actionability board for reasons no validation supports. Conviction now moves
only on the two justified axes, so plan order ≡ board order within an RS tier.

Then allocations are constrained by:
    • regime exposure cap   (Phase 6: STRONG_BULL 100% … STRONG_BEAR 10%, VOLATILE halved)
    • sector cap            (default 30% — stops "all 5 picks are Capital Goods")
    • correlation clusters  (60D corr > 0.80 → one trade, capped)
    • max positions / per-name size

Consumes Phase-5 actionability + Phase-6 regime (+ Phase-7 adaptive). Rebuilds
nothing. Classes: PositionSizer · RiskBudgetAllocator · PortfolioConstructor ·
PortfolioCandidate · PortfolioSnapshot.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from scanner.scanner import compute_atr_series
from scanner.sector_engine import week_id_for

_log = logging.getLogger(__name__)

# ── Regime → max portfolio exposure (% of capital deployed) ──────────────────
# BEAR/VOLATILE = 0 (2026-07-02 consistency fix): the E4 validation shows the
# leader edge REVERSES in BEAR (t≈−3.3 OOS), and --screen's committee gate
# already refuses fresh longs there. Allocating 10-25% here made --portfolio /
# --orders contradict the committee. ONE regime interpretation everywhere:
# no new long capital outside BULL/NEUTRAL/RANGE. (Exit/hold management of
# existing positions is never gated — that lives in the lifecycle engine.)
REGIME_EXPOSURE = {
    "STRONG_BULL": 100, "BULL": 75, "NEUTRAL": 50, "RANGE": 40,
    "WEAK_BEAR": 0, "STRONG_BEAR": 0, "VOLATILE": 0,
}

# ── Limits (configurable) ────────────────────────────────────────────────────
MAX_SECTOR_EXPOSURE = 30.0     # % of capital in any one sector
MAX_CLUSTER_EXPOSURE = 20.0    # % in any one 60D-correlation cluster
MAX_POSITIONS = 8
MAX_POSITION_SIZE = 10.0       # % capital per name (hard ceiling)
MIN_POSITION_SIZE = 1.5        # skip dust allocations
CORRELATION_THRESHOLD = 0.80
ELIGIBLE_CLASSES = {"ACTION_NOW", "WATCHLIST"}   # EXTENDED/AVOID get no capital

# ── Risk-based sizing (Phase 10 Priority 1) ──────────────────────────────────
# Size from RISK, not money: a position risks (conviction-scaled) % of capital,
# so size = risk_budget / stop_distance. A wide stop ⇒ smaller position; a tight
# stop ⇒ larger position (up to the MAX_POSITION_SIZE ceiling), at equal risk.
RISK_PER_TRADE_MAX = 1.0       # % capital risked on a max-conviction trade
MIN_STOP_PCT = 1.0             # floor on stop distance to avoid blow-up sizing

# ── Theme cap (G8) — mirrors lifecycle_engine; enforced here in construction ─
MAX_THEME_EXPOSURE = 40.0      # % of capital in any one macro-theme complex
_THEME_PATH_PE = Path("config/theme_map.yaml")
_DEFAULT_THEME_MAP_PE = {
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

# ── Conviction component maps ────────────────────────────────────────────────
_RS_SCORE = {"MARKET_LEADER": 100, "SECTOR_LEADER": 85, "EMERGING_LEADER": 65,
             "NEUTRAL": 40, "LAGGARD": 10}
_GRADE_SCORE = {"A+": 100, "A": 85, "B+": 70, "B": 55, "C": 40, "D": 20}

_HISTORY_DIR = Path("Journal/portfolio_history")
_REPORTS_DIR = Path("reports")


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _load_theme_sector_map() -> dict[str, str]:
    """Return {sector_name: theme_name} for theme-cap enforcement in the allocator.
    Standalone loader — does NOT import from lifecycle_engine (avoids circular import)."""
    try:
        text = _THEME_PATH_PE.read_text(encoding="utf-8") if _THEME_PATH_PE.exists() else ""
        if text:
            data: dict[str, list[str]] = {}
            cur: Optional[str] = None
            for raw in text.splitlines():
                line = raw.split("#", 1)[0].rstrip()
                if not line.strip():
                    continue
                if not line.startswith((" ", "\t")) and line.rstrip().endswith(":"):
                    cur = line.strip()[:-1].strip()
                    data[cur] = []
                elif line.strip().startswith("- ") and cur is not None:
                    data[cur].append(line.strip()[2:].strip())
            if data:
                return {s: th for th, secs in data.items() for s in secs}
    except Exception:
        pass
    return {s: th for th, secs in _DEFAULT_THEME_MAP_PE.items() for s in secs}


# ═════════════════════════════════════════════════════════════════════════════
# POSITION SIZER
# ═════════════════════════════════════════════════════════════════════════════
class PositionSizer:
    """Conviction (RS-anchored) → desired position size %. Independent.

    `risk_per_trade` is the % of capital risked on a MAX-conviction trade (the
    trader's profile setting; default RISK_PER_TRADE_MAX). Every position's size
    derives from it: size% = (conviction-scaled risk%) / stop-distance%."""

    def __init__(self, risk_per_trade: float = RISK_PER_TRADE_MAX):
        self.risk_per_trade = float(risk_per_trade)

    def conviction(self, rs_status: str, actionability_score: float,
                   rr: float, grade: str) -> float:
        # `rr` stays in the signature for call-site compatibility but carries
        # ZERO weight — proven non-predictive OOS (analytics/rr_validation).
        rs = _RS_SCORE.get(rs_status, 40)
        base = 0.55 * rs + 0.45 * actionability_score
        grade_mod = (_GRADE_SCORE.get(grade, 50) - 50) * 0.10   # ≤ ±5 (secondary)
        return round(_clamp(base + grade_mod), 1)

    def risk_budget(self, conviction: float) -> float:
        """% of capital to RISK on this trade, scaled by conviction (≤ risk_per_trade)."""
        if conviction < 30:
            return 0.0
        return round((conviction / 100.0) * self.risk_per_trade, 3)

    def size_by_risk(self, conviction: float, stop_pct: float) -> float:
        """position % = risk_budget% / stop_distance% (capped). Wide stop → small size."""
        rb = self.risk_budget(conviction)
        if rb <= 0:
            return 0.0
        sp = max(stop_pct, MIN_STOP_PCT)
        size = rb * 100.0 / sp        # so size% × sp%/100 == rb%
        return round(min(size, MAX_POSITION_SIZE), 2)


# ═════════════════════════════════════════════════════════════════════════════
# RISK BUDGET ALLOCATOR
# ═════════════════════════════════════════════════════════════════════════════
class RiskBudgetAllocator:
    """Greedy, conviction-ordered fill of the exposure budget under all limits."""

    def allocate(self, candidates: list["PortfolioCandidate"], exposure: float,
                 max_sector: float = MAX_SECTOR_EXPOSURE,
                 max_cluster: float = MAX_CLUSTER_EXPOSURE,
                 max_positions: int = MAX_POSITIONS,
                 sector_to_theme: Optional[dict] = None,
                 max_theme: float = MAX_THEME_EXPOSURE) -> tuple[list, float]:
        total = 0.0
        sector_exp: dict[str, float] = {}
        cluster_exp: dict[str, float] = {}
        theme_exp: dict[str, float] = {}
        chosen: list[PortfolioCandidate] = []

        for c in candidates:                 # already sorted by conviction desc
            if len(chosen) >= max_positions:
                c.skip_reason = f"max positions ({max_positions}) reached"
                continue
            if total >= exposure:
                c.skip_reason = (f"regime exposure budget ({exposure:.0f}%) fully "
                                 f"deployed")
                continue
            if c.desired_size <= 0:
                c.skip_reason = "conviction below sizing floor (desired size 0)"
                continue
            theme = (sector_to_theme or {}).get(c.sector, c.sector)
            room = min(c.desired_size,
                       max_sector - sector_exp.get(c.sector, 0.0),
                       max_cluster - cluster_exp.get(c.cluster_id, 0.0),
                       max_theme  - theme_exp.get(theme, 0.0),
                       exposure - total)
            if room < MIN_POSITION_SIZE:
                # name the binding limit so the decision trace can explain it
                binding = min(
                    (("sector cap", max_sector - sector_exp.get(c.sector, 0.0)),
                     ("cluster cap", max_cluster - cluster_exp.get(c.cluster_id, 0.0)),
                     ("theme cap", max_theme - theme_exp.get(theme, 0.0)),
                     ("exposure budget", exposure - total)),
                    key=lambda kv: kv[1])[0]
                c.skip_reason = f"capped out by {binding} (room {room:.1f}% < min {MIN_POSITION_SIZE}%)"
                continue
            c.allocation = round(room, 2)
            c.theme = theme                  # store for downstream display
            chosen.append(c)
            total += c.allocation
            sector_exp[c.sector] = sector_exp.get(c.sector, 0.0) + c.allocation
            cluster_exp[c.cluster_id] = cluster_exp.get(c.cluster_id, 0.0) + c.allocation
            theme_exp[theme] = theme_exp.get(theme, 0.0) + c.allocation
        return chosen, round(total, 2)


# ═════════════════════════════════════════════════════════════════════════════
# OUTPUT TYPES
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class PortfolioCandidate:
    ticker:              str
    symbol:              str
    sector:              str
    rs_status:           str
    classification:      str
    conviction:          float
    actionability_score: float
    composite_score:     float
    grade:               str
    rr:                  float
    atr_pct:             Optional[float]
    cluster_id:          str
    desired_size:        float
    entry:               float = 0.0
    stop:                float = 0.0
    stop_pct:            float = 0.0    # risk % = (entry − stop) / entry
    target:              float = 0.0    # T1 from actionability engine (G4)
    allocation:          float = 0.0
    risk_contribution:   float = 0.0    # allocation × stop_pct / 100 (capital at risk)
    theme:               str   = ""     # macro-theme complex (G8)
    skip_reason:         str   = ""     # why the allocator passed on it (decision trace)


@dataclass
class PortfolioSnapshot:
    generated_at:         str
    week_id:              str
    regime:               str
    recommended_exposure: float
    deployed:             float
    cash:                 float
    positions:            list[PortfolioCandidate]
    clusters:             dict           # cluster_id -> [tickers] (size>1 only)
    metrics:              dict

    # ── Serialisation ────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at, "week_id": self.week_id,
            "regime": self.regime, "recommended_exposure": self.recommended_exposure,
            "deployed": self.deployed, "cash": self.cash,
            "positions": [
                {"ticker": p.symbol, "allocation": p.allocation, "sector": p.sector,
                 "conviction": p.conviction, "rr": p.rr, "entry": p.entry, "stop": p.stop,
                 "target": p.target, "risk_pct": p.stop_pct,
                 "risk_contribution": p.risk_contribution,
                 "rs_status": p.rs_status, "classification": p.classification,
                 "cluster": p.cluster_id, "theme": p.theme}
                for p in self.positions],
            "clusters": self.clusters, "metrics": self.metrics,
        }

    def export_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def to_csv(self) -> str:
        rows = [{"ticker": p.symbol, "sector": p.sector, "allocation_pct": p.allocation,
                 "risk_pct": p.stop_pct, "risk_contribution": p.risk_contribution,
                 "conviction": p.conviction, "rr": p.rr, "entry": p.entry, "stop": p.stop,
                 "target": p.target, "rs_status": p.rs_status,
                 "classification": p.classification,
                 "grade": p.grade, "cluster": p.cluster_id,
                 "theme": p.theme} for p in self.positions]
        return pd.DataFrame(rows).to_csv(index=False)

    def to_markdown(self) -> str:
        m = self.metrics
        L = [f"# Portfolio — {self.generated_at[:10]}", "",
             f"**Regime: {self.regime} · Recommended exposure: {self.recommended_exposure}% "
             f"· Deployed: {self.deployed}% · Cash: {self.cash}%**", "",
             f"Expected RR {m.get('expected_rr')} · Avg conviction {m.get('avg_conviction')} "
             f"· Portfolio risk {m.get('portfolio_risk_pct')}% "
             f"· Sector concentration {m.get('sector_concentration')}%", "",
             "## Allocations", "",
             "| Ticker | Sector | Alloc % | Risk % | Capital@Risk % | Entry | Stop | T1 | Conviction | RR | RS | Class |",
             "|--------|--------|--------:|-------:|---------------:|------:|-----:|---:|-----------:|---:|----|-------|"]
        for p in self.positions:
            t1 = f"{p.target:.2f}" if p.target else "—"
            L.append(f"| {p.symbol} | {p.sector} | {p.allocation} | {p.stop_pct} | "
                     f"{p.risk_contribution} | {p.entry:.2f} | {p.stop:.2f} | {t1} | "
                     f"{p.conviction} | {p.rr} | {p.rs_status} | {p.classification} |")
        if self.clusters:
            L += ["", "## Correlation clusters (60D corr > 0.80 — treat as one trade)"]
            for cid, members in self.clusters.items():
                L.append(f"- **{cid}**: {', '.join(members)}")
        return "\n".join(L) + "\n"

    def save(self, history_dir: Path = _HISTORY_DIR) -> Path:
        history_dir.mkdir(parents=True, exist_ok=True)
        path = history_dir / f"{self.week_id}.json"
        path.write_text(self.export_json())
        csv = history_dir / "portfolio_history.csv"
        try:
            rows = [{"week_id": self.week_id, "date": self.generated_at[:10],
                     "regime": self.regime, "exposure": self.recommended_exposure,
                     "deployed": self.deployed, "cash": self.cash,
                     "ticker": p.symbol, "allocation": p.allocation,
                     "conviction": p.conviction, "sector": p.sector} for p in self.positions]
            new = pd.DataFrame(rows) if rows else pd.DataFrame(
                [{"week_id": self.week_id, "date": self.generated_at[:10],
                  "regime": self.regime, "exposure": self.recommended_exposure,
                  "deployed": self.deployed, "cash": self.cash,
                  "ticker": None, "allocation": 0, "conviction": None, "sector": None}])
            if csv.exists():
                old = pd.read_csv(csv)
                old = old[old["week_id"] != self.week_id]
                new = pd.concat([old, new], ignore_index=True)
            new.to_csv(csv, index=False, encoding="utf-8")
        except Exception as exc:
            _log.warning("portfolio history csv: %s", exc)
        return path


# ═════════════════════════════════════════════════════════════════════════════
# CONSTRUCTOR
# ═════════════════════════════════════════════════════════════════════════════
class PortfolioConstructor:
    """Builds the best portfolio (not the top-N) from Phase-5/6 outputs."""

    def __init__(self, max_sector=MAX_SECTOR_EXPOSURE, max_cluster=MAX_CLUSTER_EXPOSURE,
                 max_positions=MAX_POSITIONS, candidate_pool=30,
                 risk_per_trade=RISK_PER_TRADE_MAX):
        self.sizer = PositionSizer(risk_per_trade=risk_per_trade)
        self.allocator = RiskBudgetAllocator()
        self.max_sector = max_sector
        self.max_cluster = max_cluster
        self.max_positions = max_positions
        self.candidate_pool = candidate_pool

    def construct(self, act_snap, regime_snap, ohlcv: dict[str, pd.DataFrame],
                  persist: bool = False) -> PortfolioSnapshot:
        regime = regime_snap.regime
        exposure = float(REGIME_EXPOSURE.get(regime, 50))

        # Eligible candidates (tradable classes only), best actionability first.
        eligible = [r for r in act_snap.top_n(self.candidate_pool)
                    if r.classification in ELIGIBLE_CLASSES]

        clusters = _correlation_clusters([r.ticker for r in eligible], ohlcv,
                                         CORRELATION_THRESHOLD)

        sector_to_theme = _load_theme_sector_map()

        cands: list[PortfolioCandidate] = []
        for r in eligible:
            atr_pct = _atr_pct(ohlcv.get(r.ticker))
            conv = self.sizer.conviction(r.rs_status, r.actionability_score,
                                         r.rr_ratio, r.grade)
            stop_pct = round((r.entry - r.stop) / r.entry * 100, 2) if r.entry else 0.0
            cands.append(PortfolioCandidate(
                r.ticker, r.symbol, r.sector, r.rs_status, r.classification, conv,
                r.actionability_score, r.composite_score, r.grade, r.rr_ratio, atr_pct,
                clusters.get(r.ticker, r.ticker),
                self.sizer.size_by_risk(conv, stop_pct),     # RISK-based desired size
                entry=r.entry, stop=r.stop, stop_pct=stop_pct,
                target=getattr(r, "target", 0.0)))

        cands.sort(key=lambda c: (-c.conviction, -c.actionability_score, c.ticker))
        chosen, deployed = self.allocator.allocate(
            cands, exposure, self.max_sector, self.max_cluster, self.max_positions,
            sector_to_theme=sector_to_theme, max_theme=MAX_THEME_EXPOSURE)

        for c in chosen:                                     # capital actually at risk
            c.risk_contribution = round(c.allocation * c.stop_pct / 100.0, 3)
        metrics = self._metrics(chosen, deployed)
        # Decision-trace audit: why each ELIGIBLE candidate was passed over.
        chosen_syms = {c.symbol for c in chosen}
        metrics["allocator_skips"] = {c.symbol: c.skip_reason for c in cands
                                      if c.symbol not in chosen_syms and c.skip_reason}
        cluster_groups = _cluster_groups(clusters)
        snap = PortfolioSnapshot(
            datetime.now().isoformat(timespec="seconds"), week_id_for(), regime,
            exposure, deployed, round(100 - deployed, 2), chosen, cluster_groups, metrics)
        if persist:
            snap.save()
        return snap

    @staticmethod
    def _metrics(positions, deployed) -> dict:
        if not positions:
            return {"expected_rr": None, "avg_conviction": None,
                    "sector_concentration": 0.0, "cluster_concentration": 0.0,
                    "n_positions": 0}
        w = deployed or 1.0
        exp_rr = sum(p.rr * p.allocation for p in positions) / w
        avg_conv = sum(p.conviction * p.allocation for p in positions) / w
        sec: dict[str, float] = {}
        clu: dict[str, float] = {}
        for p in positions:
            sec[p.sector] = sec.get(p.sector, 0.0) + p.allocation
            clu[p.cluster_id] = clu.get(p.cluster_id, 0.0) + p.allocation
        portfolio_risk = sum(p.risk_contribution for p in positions)
        risks = [p.risk_contribution for p in positions]
        return {"expected_rr": round(exp_rr, 2), "avg_conviction": round(avg_conv, 1),
                "sector_concentration": round(max(sec.values()), 1),
                "cluster_concentration": round(max(clu.values()), 1),
                "n_positions": len(positions),
                "portfolio_risk_pct": round(portfolio_risk, 2),
                "avg_risk_per_trade": round(sum(risks) / len(risks), 3),
                "sector_exposure": {k: round(v, 1) for k, v in
                                    sorted(sec.items(), key=lambda kv: -kv[1])}}


# ═════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═════════════════════════════════════════════════════════════════════════════
def _atr_pct(df: Optional[pd.DataFrame]) -> Optional[float]:
    if df is None or len(df) < 20:
        return None
    atr = float(compute_atr_series(df).iloc[-1])
    close = float(df["Close"].iloc[-1])
    return round(atr / close * 100, 2) if close else None


def _correlation_clusters(tickers: list[str], ohlcv: dict[str, pd.DataFrame],
                          threshold: float = CORRELATION_THRESHOLD,
                          lookback: int = 60) -> dict[str, str]:
    """Union-find clustering on 60D return correlation. cluster_id = min ticker."""
    rets: dict[str, pd.Series] = {}
    for t in tickers:
        df = ohlcv.get(t)
        if df is not None and len(df) >= lookback + 1:
            rets[t] = df["Close"].pct_change().tail(lookback)

    parent = {t: t for t in rets}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)   # deterministic: min ticker is root

    keys = sorted(rets)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            try:
                c = float(rets[a].corr(rets[b]))
            except Exception:
                c = 0.0
            if c > threshold:
                union(a, b)

    out = {t: find(t) for t in rets}
    for t in tickers:                         # tickers without data → own cluster
        out.setdefault(t, t)
    return out


def _cluster_groups(clusters: dict[str, str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for t, cid in clusters.items():
        groups.setdefault(cid, []).append(t)
    # Only clusters with >1 member are interesting; show symbols (strip .NS).
    return {cid: [m[:-3] if m.endswith(".NS") else m for m in sorted(members)]
            for cid, members in groups.items() if len(members) > 1}


# ─────────────────────────────────────────────────────────────────────────────
# RENDER + EXPORT + RUN
# ─────────────────────────────────────────────────────────────────────────────
def render_portfolio(snap: PortfolioSnapshot) -> None:
    G, R, Y, B, D, RST = ("\033[92m", "\033[91m", "\033[93m", "\033[1m", "\033[2m", "\033[0m")
    ec = {"STRONG_BULL": G, "BULL": G, "NEUTRAL": Y, "RANGE": Y,
          "WEAK_BEAR": R, "STRONG_BEAR": R, "VOLATILE": R}.get(snap.regime, "")
    print(f"\n  {B}{'═'*64}{RST}")
    print(f"  {B}  PORTFOLIO ALLOCATION{RST}")
    print(f"  {B}{'═'*64}{RST}")
    print(f"\n  {B}Market regime:{RST} {ec}{snap.regime}{RST}     "
          f"{B}Recommended exposure:{RST} {snap.recommended_exposure:.0f}%")
    print(f"  {B}Deployed:{RST} {snap.deployed:.1f}%     {B}Cash:{RST} {snap.cash:.1f}%")
    m = snap.metrics
    print(f"  {D}Expected RR {m['expected_rr']}  ·  avg conviction {m['avg_conviction']}  ·  "
          f"portfolio risk {m.get('portfolio_risk_pct')}%  ·  sector conc "
          f"{m['sector_concentration']}%  ·  {m['n_positions']} positions{RST}")

    if snap.positions:
        print(f"\n  {B}{'TICKER':12s}  {'ALLOC':>6}  {'RISK%':>6}  {'@RISK':>6}  {'CONV':>5}  "
              f"{'RR':>4}  {'RS':16s}  {'SECTOR':20s}  CLASS{RST}")
        for p in snap.positions:
            print(f"  {p.symbol[:12]:12s}  {G}{p.allocation:>5.1f}%{RST}  {p.stop_pct:>5.1f}%  "
                  f"{p.risk_contribution:>5.2f}  {p.conviction:>5.0f}  {p.rr:>4.1f}  "
                  f"{p.rs_status[:16]:16s}  {p.sector[:20]:20s}  {p.classification}")
    else:
        print(f"\n  {Y}No positions — regime too weak / no qualifying setups. Stay in cash.{RST}")

    if m.get("sector_exposure"):
        print(f"\n  {B}Sector exposure{RST}")
        print("  " + "   ".join(f"{s} {v:.0f}%" for s, v in m["sector_exposure"].items()))

    if snap.clusters:
        print(f"\n  {B}Correlation warnings{RST}  {D}(60D corr > 0.80 → one trade){RST}")
        for cid, members in snap.clusters.items():
            in_port = [p.symbol for p in snap.positions if p.cluster_id == cid]
            tag = f"  {Y}← {len(in_port)} held{RST}" if len(in_port) > 1 else ""
            print(f"    {', '.join(members)}{tag}")
    print()


def export_portfolio(snap: PortfolioSnapshot, reports_dir: Path = _REPORTS_DIR) -> dict:
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    paths = {"json": reports_dir / f"portfolio_{stamp}.json",
             "csv": reports_dir / f"portfolio_{stamp}.csv",
             "md": reports_dir / f"portfolio_{stamp}.md"}
    paths["json"].write_text(snap.export_json(), encoding="utf-8")
    paths["csv"].write_text(snap.to_csv(), encoding="utf-8")
    paths["md"].write_text(snap.to_markdown(), encoding="utf-8")
    return paths


def run_portfolio(force_refresh: bool = False) -> int:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    from screen.screen_runner import (build_screen, BenchmarkUnavailableError,
                                       render_benchmark_abort)
    print("\n  Building portfolio (screen → regime → conviction → allocation)...")
    try:
        res = build_screen(force_refresh=force_refresh, persist=True)
    except BenchmarkUnavailableError as exc:
        render_benchmark_abort(exc)
        return 2
    # Same position cap as the --screen committee plan (config/deployment.yaml).
    try:
        from portfolio.portfolio_state import load_deployment_config
        _cfg = load_deployment_config()
        _maxp = int(_cfg.get("target_positions", 5))
        _rpt = float(_cfg.get("risk_per_trade_pct", RISK_PER_TRADE_MAX))
    except Exception:
        _maxp, _rpt = MAX_POSITIONS, RISK_PER_TRADE_MAX
    snap = PortfolioConstructor(max_positions=_maxp, risk_per_trade=_rpt).construct(
        res.act_snap, res.regime, res.feed.data, persist=True)
    render_portfolio(snap)
    paths = export_portfolio(snap)
    print(f"  Exports: {paths['json']} · {paths['csv']} · {paths['md']}\n")
    return 0
