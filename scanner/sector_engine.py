"""
scanner/sector_engine.py — Sector Leadership Engine (Phase 2)
=============================================================
Professional swing traders rank sectors BEFORE stocks: a mediocre stock in a
leading sector often beats a great stock in a weak one. This engine answers
"where is institutional money flowing?" using bottom-up aggregation of the
actual Nifty 500 constituents — no ETFs, no external sector indices.

Pipeline
--------
    constituent OHLCV  →  per-stock 1M/3M/6M returns
                       →  per-sector MEDIAN return (outlier-robust)
                       →  weighted composite (6M 50% · 3M 30% · 1M 20%)
                       →  relative score (0-100) + grade (A+..C) + status
                       →  rank-delta vs last week's persisted snapshot

Design
------
• Fully decoupled from the screener — `SectorRanker(sector_map, ohlcv)` takes
  plain inputs (dependency injection), so it is independently testable.
• Deterministic — ties break on sector name, so repeated runs on the same data
  produce identical rankings.
• Persisted — `SectorSnapshot.save()` writes a weekly JSON + appends a long-form
  CSV, giving Phase 3 (Relative Strength) and Phase 7 (Regime) ready-made
  historical sector leadership instead of rebuilding it later.

Classes
-------
    SectorMetrics    per-sector aggregated returns (the raw measurement)
    SectorRow        a fully-ranked sector (score, grade, status, delta, reasons)
    SectorSnapshot   the ranked board at a point in time + persistence + lookups
    SectorRanker     orchestrates metrics → ranking → snapshot
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from statistics import median
from typing import Optional

import pandas as pd

_log = logging.getLogger(__name__)

# ── Tunables ─────────────────────────────────────────────────────────────────
# Lookbacks in TRADING days (≈21/month). Needs ~127 bars for 6M → use 1y data.
LOOKBACKS = {"r1m": 21, "r3m": 63, "r6m": 126}

# Swing trading prioritises persistent leadership over short-term noise.
COMPOSITE_WEIGHTS = {"r6m": 0.50, "r3m": 0.30, "r1m": 0.20}

# A sector needs at least this many valid constituents for a period to be trusted.
MIN_VALID_CONSTITUENTS = 3

# Status classification thresholds (percentile space, 0=worst sector .. 1=best).
_ACCEL_THRESHOLD = 0.20      # p1m − p6m beyond this → momentum (im/de)proving
_RANK_DELTA_THRESHOLD = 3    # rose/fell this many ranks → im/de proving
_STRONG_PCTL = 0.60          # "strong in a period" cutoff
_WEAK_PCTL = 0.40            # "weak in a period" cutoff

# Persistence
_HISTORY_DIR = Path("Journal/sector_history")
_HISTORY_CSV = Path("Journal/sector_history.csv")


# ─────────────────────────────────────────────────────────────────────────────
# PER-STOCK / PER-SECTOR MEASUREMENT
# ─────────────────────────────────────────────────────────────────────────────
def pct_return(df: pd.DataFrame, lookback: int) -> Optional[float]:
    """Percent price return over *lookback* trading days, or None if too short."""
    if df is None or "Close" not in df.columns or len(df) <= lookback:
        return None
    now = float(df["Close"].iloc[-1])
    then = float(df["Close"].iloc[-1 - lookback])
    if then <= 0:
        return None
    return round((now / then - 1.0) * 100.0, 2)


def compute_stock_returns(df: pd.DataFrame) -> dict[str, Optional[float]]:
    """{r1m, r3m, r6m} for one stock (any may be None)."""
    return {k: pct_return(df, lb) for k, lb in LOOKBACKS.items()}


@dataclass
class SectorMetrics:
    """Median-aggregated returns for one sector — the raw measurement."""
    sector:         str
    n_constituents: int
    n_valid:        int
    r1m:            Optional[float]
    r3m:            Optional[float]
    r6m:            Optional[float]
    composite:      Optional[float]

    @classmethod
    def aggregate(cls, sector: str, n_constituents: int,
                  constituent_returns: list[dict[str, Optional[float]]]
                  ) -> "SectorMetrics":
        """Median across constituents per period; composite over available periods."""
        agg: dict[str, Optional[float]] = {}
        for period in LOOKBACKS:
            vals = [r[period] for r in constituent_returns if r.get(period) is not None]
            agg[period] = round(median(vals), 2) if len(vals) >= MIN_VALID_CONSTITUENTS else None

        # Composite over whatever periods are available, re-normalising weights.
        num = den = 0.0
        for period, w in COMPOSITE_WEIGHTS.items():
            if agg[period] is not None:
                num += w * agg[period]
                den += w
        composite = round(num / den, 2) if den > 0 else None

        n_valid = sum(1 for r in constituent_returns
                      if any(v is not None for v in r.values()))
        return cls(sector, n_constituents, n_valid,
                   agg["r1m"], agg["r3m"], agg["r6m"], composite)


# ─────────────────────────────────────────────────────────────────────────────
# RANKED OUTPUT TYPES
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class SectorRow:
    """A fully-ranked sector — everything needed for explainable output."""
    sector:         str
    rank:           int
    score:          float            # 0-100, relative to current universe
    grade:          str              # A+ / A / B+ / B / C
    status:         str              # LEADING / IMPROVING / NEUTRAL / WEAKENING / LAGGING
    r1m:            Optional[float]
    r3m:            Optional[float]
    r6m:            Optional[float]
    composite:      Optional[float]
    n_constituents: int
    n_valid:        int
    rank_delta:     Optional[int]    # vs previous snapshot (+ = moved up)
    reasons:        list[str] = field(default_factory=list)


@dataclass
class SectorSnapshot:
    """The ranked sector board at a point in time, with persistence + lookups."""
    rows:            list[SectorRow]
    generated_at:    str
    week_id:         str
    universe_source: str = ""

    # ── Lookups (consumed by Phase 3 / Phase 7) ──────────────────────────────
    def by_sector(self) -> dict[str, SectorRow]:
        return {r.sector: r for r in self.rows}

    def get(self, sector: str) -> Optional[SectorRow]:
        return self.by_sector().get(sector)

    def score_of(self, sector: str, default: float = 50.0) -> float:
        r = self.get(sector)
        return r.score if r else default

    def rank_of(self, sector: str) -> Optional[int]:
        r = self.get(sector)
        return r.rank if r else None

    # ── Serialisation ────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "generated_at":    self.generated_at,
            "week_id":         self.week_id,
            "universe_source": self.universe_source,
            "rows":            [asdict(r) for r in self.rows],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SectorSnapshot":
        rows = [SectorRow(**r) for r in d.get("rows", [])]
        return cls(rows, d.get("generated_at", ""), d.get("week_id", ""),
                   d.get("universe_source", ""))

    def save(self, history_dir: Path = _HISTORY_DIR,
             history_csv: Path = _HISTORY_CSV) -> Path:
        """Persist one weekly snapshot (JSON) and append/update the long-form CSV."""
        history_dir.mkdir(parents=True, exist_ok=True)
        path = history_dir / f"{self.week_id}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2))
        self._append_csv(history_csv)
        return path

    def _append_csv(self, history_csv: Path) -> None:
        try:
            new = pd.DataFrame([{
                "week_id": self.week_id, "date": self.generated_at[:10],
                "sector": r.sector, "rank": r.rank, "score": r.score,
                "grade": r.grade, "status": r.status,
                "r1m": r.r1m, "r3m": r.r3m, "r6m": r.r6m,
                "composite": r.composite,
            } for r in self.rows])
            if history_csv.exists():
                old = pd.read_csv(history_csv)
                old = old[old["week_id"] != self.week_id]   # replace this week
                new = pd.concat([old, new], ignore_index=True)
            history_csv.parent.mkdir(parents=True, exist_ok=True)
            new.to_csv(history_csv, index=False, encoding="utf-8")
        except Exception as exc:
            _log.warning("Could not append sector history CSV: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# PERSISTENCE HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def week_id_for(d: Optional[date] = None) -> str:
    """ISO-week identifier, e.g. '2026-W24'."""
    d = d or date.today()
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def load_previous_snapshot(before_week: str,
                           history_dir: Path = _HISTORY_DIR
                           ) -> Optional[SectorSnapshot]:
    """Most recent persisted snapshot from a week strictly before *before_week*."""
    if not history_dir.exists():
        return None
    candidates = sorted(p.stem for p in history_dir.glob("*.json")
                        if p.stem < before_week)
    if not candidates:
        return None
    try:
        data = json.loads((history_dir / f"{candidates[-1]}.json").read_text())
        return SectorSnapshot.from_dict(data)
    except Exception as exc:
        _log.warning("Could not load previous sector snapshot: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# RANKER
# ─────────────────────────────────────────────────────────────────────────────
class SectorRanker:
    """
    Turns (sector_map, ohlcv) into a ranked SectorSnapshot.

    Parameters
    ----------
    sector_map : {ticker: sector}          — from Universe.sector_map
    ohlcv      : {ticker: DataFrame}        — from data_feed.get_ohlcv (1y period)
    universe_source : provenance string for the snapshot (optional)
    """

    def __init__(self, sector_map: dict[str, str], ohlcv: dict[str, pd.DataFrame],
                 universe_source: str = ""):
        self.sector_map = sector_map
        self.ohlcv = ohlcv
        self.universe_source = universe_source

    # ── Step 1: measure ──────────────────────────────────────────────────────
    def compute_metrics(self) -> dict[str, SectorMetrics]:
        # Group constituents by sector.
        members: dict[str, list[str]] = {}
        for ticker, sector in self.sector_map.items():
            members.setdefault(sector, []).append(ticker)

        metrics: dict[str, SectorMetrics] = {}
        for sector, tickers in members.items():
            returns = [compute_stock_returns(self.ohlcv[t])
                       for t in tickers if t in self.ohlcv]
            metrics[sector] = SectorMetrics.aggregate(sector, len(tickers), returns)
        return metrics

    # ── Step 2: rank + classify ──────────────────────────────────────────────
    def rank(self, persist: bool = False) -> SectorSnapshot:
        metrics = self.compute_metrics()
        wk = week_id_for()
        prev = load_previous_snapshot(wk)
        prev_ranks = ({r.sector: r.rank for r in prev.rows} if prev else {})

        valid = [m for m in metrics.values() if m.composite is not None]
        invalid = [m for m in metrics.values() if m.composite is None]

        # Deterministic ordering: composite desc, then sector name asc.
        valid.sort(key=lambda m: (-m.composite, m.sector))
        invalid.sort(key=lambda m: m.sector)

        k = len(valid)
        cmin = min((m.composite for m in valid), default=0.0)
        cmax = max((m.composite for m in valid), default=0.0)
        pmaps = {p: self._percentiles(valid, p) for p in LOOKBACKS}

        rows: list[SectorRow] = []
        for i, m in enumerate(valid):
            rank = i + 1
            score = 50.0 if cmax == cmin else round(100 * (m.composite - cmin) / (cmax - cmin), 1)
            grade = self._grade(rank, k)
            delta = (prev_ranks[m.sector] - rank) if m.sector in prev_ranks else None
            status, reasons = self._classify(
                m, p1=pmaps["r1m"].get(m.sector, 0.5),
                p3=pmaps["r3m"].get(m.sector, 0.5),
                p6=pmaps["r6m"].get(m.sector, 0.5),
                comp_pctl=(1.0 if k <= 1 else 1 - (rank - 1) / (k - 1)),
                rank_delta=delta,
            )
            rows.append(SectorRow(
                m.sector, rank, score, grade, status,
                m.r1m, m.r3m, m.r6m, m.composite,
                m.n_constituents, m.n_valid, delta, reasons,
            ))

        # Insufficient-data sectors are listed (never skipped) at the bottom.
        for j, m in enumerate(invalid):
            rows.append(SectorRow(
                m.sector, k + j + 1, 0.0, "C", "NEUTRAL",
                m.r1m, m.r3m, m.r6m, m.composite,
                m.n_constituents, m.n_valid, None,
                [f"Insufficient data ({m.n_valid}/{m.n_constituents} valid)"],
            ))

        snap = SectorSnapshot(rows, datetime.now().isoformat(timespec="seconds"),
                              wk, self.universe_source)
        if persist:
            snap.save()
        return snap

    # ── Classification & grading ─────────────────────────────────────────────
    @staticmethod
    def _percentiles(metrics: list[SectorMetrics], period: str) -> dict[str, float]:
        """Rank-percentile per sector for one period (1=best return, 0=worst)."""
        vals = [(m.sector, getattr(m, period)) for m in metrics
                if getattr(m, period) is not None]
        vals.sort(key=lambda x: (-x[1], x[0]))
        n = len(vals)
        return {s: (1.0 if n <= 1 else 1 - i / (n - 1)) for i, (s, _) in enumerate(vals)}

    @staticmethod
    def _grade(rank: int, k: int) -> str:
        """Relative grade by position: top 10% A+, next 15% A, 25% B+, 25% B, rest C."""
        if k <= 0:
            return "C"
        p = (rank - 1) / k
        if p < 0.10:
            return "A+"
        if p < 0.25:
            return "A"
        if p < 0.50:
            return "B+"
        if p < 0.75:
            return "B"
        return "C"

    @staticmethod
    def _classify(m: SectorMetrics, p1: float, p3: float, p6: float,
                  comp_pctl: float, rank_delta: Optional[int]
                  ) -> tuple[str, list[str]]:
        """
        Assign LEADING / IMPROVING / NEUTRAL / WEAKENING / LAGGING with reasons.

        Uses percentile structure (history-optional) plus rank-delta when a
        previous snapshot exists. accel = short-term percentile minus long-term:
        positive ⇒ leadership building, negative ⇒ leadership fading.
        """
        accel = p1 - p6
        reasons: list[str] = []

        def _r(period: str, val: Optional[float]) -> str:
            return f"{period} {val:+.1f}%" if val is not None else f"{period} n/a"

        strong_all = p1 >= _STRONG_PCTL and p3 >= _STRONG_PCTL and p6 >= _STRONG_PCTL
        weak_all = p1 <= _WEAK_PCTL and p3 <= _WEAK_PCTL and p6 <= _WEAK_PCTL
        rose = rank_delta is not None and rank_delta >= _RANK_DELTA_THRESHOLD
        fell = rank_delta is not None and rank_delta <= -_RANK_DELTA_THRESHOLD

        if comp_pctl >= 0.75 and strong_all:
            status = "LEADING"
            reasons.append("Strong across 1M/3M/6M")
        elif comp_pctl <= 0.25 and weak_all:
            status = "LAGGING"
            reasons.append("Weak across all periods")
        elif accel >= _ACCEL_THRESHOLD or rose:
            status = "IMPROVING"
            reasons.append("Short-term momentum building"
                           if accel >= _ACCEL_THRESHOLD else "Climbing the rankings")
        elif accel <= -_ACCEL_THRESHOLD or fell:
            status = "WEAKENING"
            reasons.append("Recent leadership fading"
                           if accel <= -_ACCEL_THRESHOLD else "Slipping in the rankings")
        else:
            status = "NEUTRAL"

        reasons.append(", ".join(_r(p, v) for p, v in
                                 (("6M", m.r6m), ("3M", m.r3m), ("1M", m.r1m))))
        if rank_delta:
            reasons.append(f"{'↑' if rank_delta > 0 else '↓'} {abs(rank_delta)} ranks WoW")
        return status, reasons
