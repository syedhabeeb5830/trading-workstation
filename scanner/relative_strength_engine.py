"""
scanner/relative_strength_engine.py — Relative Strength Engine (Phase 3)
========================================================================
Surfaces "leaders among leaders": stocks outperforming BOTH the broad market
(Nifty) AND their own sector peers, persistently across timeframes. This is
true relative strength — not raw momentum, not return sorting.

    RS_vs_Nifty[t]  = stock_return[t]  − nifty_return[t]
    RS_vs_Sector[t] = stock_return[t]  − sector_return[t]   (sector from Phase 2)

A stock ranks highly only when it beats the index AND its peers. A stock merely
riding a strong sector (RS_vs_Sector ≈ 0) does not earn a top score.

Pipeline
--------
    stock 1M/3M/6M returns
      − Nifty returns        → RS vs Nifty  (per timeframe)
      − sector returns       → RS vs Sector (per timeframe)
      → composite RS (6M 50% · 3M 30% · 1M 20%, blended 50/50 nifty/sector)
      → percentile score 0-100  +  grade  +  classification
      → RS buckets: TOP_10 / TOP_20 / TOP_50 / BOTTOM_50
      → rank-delta vs last week's persisted snapshot

The buckets exist so Phase 4 can let only the upper RS tiers compete for A/A+
grades — far cleaner swing candidates than letting weak names earn high scores
from ATR or trend alone.

Decoupled by design: `RelativeStrengthRanker(ohlcv, sector_map, nifty_df,
sector_snapshot)` takes plain inputs, so it is independently testable.

Classes
-------
    RSMetrics                    per-stock RS measurement (raw)
    RSRow                        a fully-ranked stock (score/grade/status/bucket)
    RelativeStrengthSnapshot     ranked board + persistence + lookups
    RelativeStrengthCalculator   pure per-stock RS computation
    RelativeStrengthRanker       orchestrates metrics → ranking → snapshot
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from scanner.sector_engine import (
    COMPOSITE_WEIGHTS, LOOKBACKS, SectorSnapshot,
    compute_stock_returns, week_id_for,
)

_log = logging.getLogger(__name__)

# ── Tunables ─────────────────────────────────────────────────────────────────
# Blend of the two RS benchmarks into one composite. Equal weight: a true leader
# must beat both the market and its peers.
RS_BLEND = {"nifty": 0.50, "sector": 0.50}

# Classification thresholds (percentile space among eligible stocks, 1=best).
_TOP_DECILE      = 0.10
_STRONG_PCTL     = 0.80
_MODERATE_PCTL   = 0.50
_EMERGING_DELTA  = 50      # rose ≥ this many RS ranks WoW → emerging

# Eligibility: a stock needs the 3M anchor at minimum (≈64 bars).
_MIN_ANCHOR = "r3m"

# Persistence
_HISTORY_DIR = Path("Journal/rs_history")
_HISTORY_CSV = Path("Journal/rs_history.csv")


# ─────────────────────────────────────────────────────────────────────────────
# SHARED HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def weighted_composite(values: dict[str, Optional[float]],
                       weights: dict[str, float] = COMPOSITE_WEIGHTS
                       ) -> Optional[float]:
    """Weighted blend over available periods, re-normalising weights. None if empty."""
    num = den = 0.0
    for period, w in weights.items():
        v = values.get(period)
        if v is not None:
            num += w * v
            den += w
    return round(num / den, 3) if den > 0 else None


def _percentiles(pairs: list[tuple[str, Optional[float]]]) -> dict[str, float]:
    """Rank-percentile per key (1=highest value, 0=lowest). Skips None values."""
    vals = [(k, v) for k, v in pairs if v is not None]
    vals.sort(key=lambda x: (-x[1], x[0]))
    n = len(vals)
    return {k: (1.0 if n <= 1 else 1 - i / (n - 1)) for i, (k, _) in enumerate(vals)}


# ─────────────────────────────────────────────────────────────────────────────
# BENCHMARK FETCH + RESILIENCE CACHE  (Phase: Data Resilience Hardening)
# ─────────────────────────────────────────────────────────────────────────────
# Benchmarks (^NSEI / ^CRSLDX / ^INDIAVIX) now mirror the stock-OHLCV resilience
# pattern: a live download is cached write-through; if the live fetch fails we
# serve the last good cache; only if BOTH fail do we report "missing" so callers
# can degrade gracefully — never crash, and never silently treat a download
# hiccup as a bearish market.
_BENCH_CACHE_DIR = Path("cache/benchmarks")


@dataclass
class BenchmarkResult:
    """A benchmark fetch outcome: the frame plus where it came from."""
    ticker: str
    df:     Optional[pd.DataFrame]
    status: str            # "live" | "cache" | "missing"


def _bench_cache_path(ticker: str, period: str) -> Path:
    safe = ticker.replace("^", "").replace("/", "_").replace("\\", "_")
    return _BENCH_CACHE_DIR / f"{safe}_{period}.csv"


def _save_bench_cache(ticker: str, period: str, df: pd.DataFrame) -> None:
    try:
        _BENCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        f = df.copy()
        f.index.name = "Date"
        f.reset_index().to_csv(_bench_cache_path(ticker, period),
                               index=False, encoding="utf-8")
    except Exception as exc:
        _log.warning("Could not write benchmark cache for %s: %s", ticker, exc)


def _load_bench_cache(ticker: str, period: str) -> Optional[pd.DataFrame]:
    path = _bench_cache_path(ticker, period)
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, parse_dates=["Date"]).set_index("Date").sort_index()
        return df if df is not None and not df.empty else None
    except Exception as exc:
        _log.warning("Could not read benchmark cache for %s: %s", ticker, exc)
        return None


def _download_benchmark(period: str, ticker: str) -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf
        df = yf.download(ticker, period=period, interval="1d",
                         auto_adjust=True, progress=False)
        if df is None or df.empty:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna()
    except Exception as exc:
        _log.warning("Could not fetch benchmark %s: %s", ticker, exc)
        return None


def fetch_benchmark(period: str = "1y", ticker: str = "^NSEI") -> BenchmarkResult:
    """Resilient benchmark fetch:

        live download  →  write-through cache  →  status="live"
             ↓ on failure
        latest cache                            →  status="cache"
             ↓ on failure
        df=None                                 →  status="missing"

    Benchmarks always try live first (one cheap download); the cache is a
    disaster-recovery fallback, not a same-day speed cache, so the healthy path
    is byte-identical to the previous behaviour.
    """
    live = _download_benchmark(period, ticker)
    if live is not None and not live.empty:
        _save_bench_cache(ticker, period, live)
        return BenchmarkResult(ticker, live, "live")
    cached = _load_bench_cache(ticker, period)
    if cached is not None and not cached.empty:
        _log.warning("Benchmark %s live fetch failed — serving cached data.", ticker)
        return BenchmarkResult(ticker, cached, "cache")
    _log.warning("Benchmark %s unavailable (live AND cache both failed).", ticker)
    return BenchmarkResult(ticker, None, "missing")


def fetch_nifty(period: str = "1y", ticker: str = "^NSEI") -> Optional[pd.DataFrame]:
    """Backward-compatible benchmark fetch — returns the DataFrame (or None).
    Now cache-backed via fetch_benchmark; existing callers are unchanged."""
    return fetch_benchmark(period=period, ticker=ticker).df


# ─────────────────────────────────────────────────────────────────────────────
# MEASUREMENT
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class RSMetrics:
    """Per-stock relative-strength measurement (before ranking)."""
    symbol:         str
    ticker:         str
    sector:         str
    r1m:            Optional[float]
    r3m:            Optional[float]
    r6m:            Optional[float]
    rs_nifty_1m:    Optional[float]
    rs_nifty_3m:    Optional[float]
    rs_nifty_6m:    Optional[float]
    rs_sector_1m:   Optional[float]
    rs_sector_3m:   Optional[float]
    rs_sector_6m:   Optional[float]
    comp_rs_nifty:  Optional[float]
    comp_rs_sector: Optional[float]
    comp_rs:        Optional[float]
    eligible:       bool


class RelativeStrengthCalculator:
    """Pure per-stock RS computation against injected Nifty + sector returns."""

    def __init__(self, nifty_returns: dict[str, Optional[float]],
                 sector_returns: dict[str, dict[str, Optional[float]]]):
        self.nifty_returns = nifty_returns
        self.sector_returns = sector_returns

    def compute(self, ticker: str, sector: str,
                df: pd.DataFrame) -> RSMetrics:
        sym = ticker[:-3] if ticker.upper().endswith(".NS") else ticker
        stock = compute_stock_returns(df)
        sec = self.sector_returns.get(sector, {})

        def _diff(period: str, bench: dict) -> Optional[float]:
            a, b = stock.get(period), bench.get(period)
            return round(a - b, 2) if (a is not None and b is not None) else None

        rs_nifty = {p: _diff(p, self.nifty_returns) for p in LOOKBACKS}
        rs_sector = {p: _diff(p, sec) for p in LOOKBACKS}

        comp_n = weighted_composite(rs_nifty)
        comp_s = weighted_composite(rs_sector)

        # Blend; fall back to whichever benchmark is available.
        if comp_n is not None and comp_s is not None:
            comp = round(RS_BLEND["nifty"] * comp_n + RS_BLEND["sector"] * comp_s, 3)
        else:
            comp = comp_n if comp_n is not None else comp_s

        # Data-resilience: a stock is eligible if it has its own history AND a
        # composite RS — which is benchmark-blended when the Nifty benchmark is
        # available, else the sector-relative fallback (comp_s, computed above).
        # A missing benchmark must NOT make every stock ineligible: that forced
        # the whole board to NEUTRAL and read as a false bearish regime.
        # NOTE: when the benchmark IS present, nifty_returns[_MIN_ANCHOR] is not
        # None for every stock, so dropping that clause changes nothing — the
        # behaviour difference is confined entirely to the benchmark-missing path.
        eligible = (stock.get(_MIN_ANCHOR) is not None
                    and comp is not None)

        return RSMetrics(
            sym, ticker, sector,
            stock["r1m"], stock["r3m"], stock["r6m"],
            rs_nifty["r1m"], rs_nifty["r3m"], rs_nifty["r6m"],
            rs_sector["r1m"], rs_sector["r3m"], rs_sector["r6m"],
            comp_n, comp_s, comp, eligible,
        )


# ─────────────────────────────────────────────────────────────────────────────
# RANKED OUTPUT TYPES
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class RSRow:
    symbol:         str
    ticker:         str
    sector:         str
    rank:           Optional[int]    # None = ineligible (insufficient history)
    score:          float            # 0-100 percentile of composite RS
    grade:          str              # A+ / A / B+ / B / C
    status:         str              # MARKET_LEADER / SECTOR_LEADER / EMERGING_LEADER / NEUTRAL / LAGGARD
    bucket:         str              # TOP_10 / TOP_20 / TOP_50 / BOTTOM_50 / NA
    percentile:     Optional[float]  # 0=best .. 1=worst (rank-based)
    rs_nifty:       Optional[float]  # composite RS vs Nifty
    rs_sector:      Optional[float]  # composite RS vs Sector
    rs_nifty_1m:    Optional[float]
    rs_nifty_3m:    Optional[float]
    rs_nifty_6m:    Optional[float]
    rs_sector_1m:   Optional[float]
    rs_sector_3m:   Optional[float]
    rs_sector_6m:   Optional[float]
    rank_delta:     Optional[int]
    reasons:        list[str] = field(default_factory=list)


@dataclass
class RelativeStrengthSnapshot:
    rows:            list[RSRow]
    generated_at:    str
    week_id:         str
    benchmark:       str = "^NSEI"
    rs_mode:         str = "FULL"            # FULL | SECTOR_FALLBACK (benchmark missing)
    benchmark_status: str = "OK"            # OK | MISSING

    # ── Lookups (consumed by Phase 4+) ───────────────────────────────────────
    def by_symbol(self) -> dict[str, RSRow]:
        return {r.ticker: r for r in self.rows}

    def get(self, ticker: str) -> Optional[RSRow]:
        return self.by_symbol().get(ticker)

    def score_of(self, ticker: str, default: float = 0.0) -> float:
        r = self.get(ticker)
        return r.score if r else default

    def rank_of(self, ticker: str) -> Optional[int]:
        r = self.get(ticker)
        return r.rank if r else None

    def bucket_of(self, ticker: str, default: str = "NA") -> str:
        r = self.get(ticker)
        return r.bucket if r else default

    @property
    def ranked(self) -> list[RSRow]:
        """Eligible, ranked rows only (sorted by rank)."""
        return sorted((r for r in self.rows if r.rank is not None),
                      key=lambda r: r.rank)

    def symbols_in_top(self, percent: float) -> set[str]:
        """Tickers whose RS percentile places them in the top *percent* (e.g. 0.20)."""
        return {r.ticker for r in self.rows
                if r.percentile is not None and r.percentile < percent}

    # ── Serialisation ────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at, "week_id": self.week_id,
            "benchmark": self.benchmark, "rs_mode": self.rs_mode,
            "benchmark_status": self.benchmark_status,
            "rows": [asdict(r) for r in self.rows],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RelativeStrengthSnapshot":
        rows = [RSRow(**r) for r in d.get("rows", [])]
        return cls(rows, d.get("generated_at", ""), d.get("week_id", ""),
                   d.get("benchmark", "^NSEI"),
                   d.get("rs_mode", "FULL"), d.get("benchmark_status", "OK"))

    def save(self, history_dir: Path = _HISTORY_DIR,
             history_csv: Path = _HISTORY_CSV) -> Path:
        history_dir.mkdir(parents=True, exist_ok=True)
        path = history_dir / f"{self.week_id}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2))
        self._append_csv(history_csv)
        return path

    def _append_csv(self, history_csv: Path) -> None:
        try:
            new = pd.DataFrame([{
                "week_id": self.week_id, "date": self.generated_at[:10],
                "ticker": r.ticker, "sector": r.sector, "rank": r.rank,
                "score": r.score, "grade": r.grade, "status": r.status,
                "bucket": r.bucket, "rs_nifty": r.rs_nifty, "rs_sector": r.rs_sector,
            } for r in self.rows])
            if history_csv.exists():
                old = pd.read_csv(history_csv)
                old = old[old["week_id"] != self.week_id]
                new = pd.concat([old, new], ignore_index=True)
            history_csv.parent.mkdir(parents=True, exist_ok=True)
            new.to_csv(history_csv, index=False, encoding="utf-8")
        except Exception as exc:
            _log.warning("Could not append RS history CSV: %s", exc)


def load_previous_snapshot(before_week: str, history_dir: Path = _HISTORY_DIR
                           ) -> Optional[RelativeStrengthSnapshot]:
    if not history_dir.exists():
        return None
    candidates = sorted(p.stem for p in history_dir.glob("*.json")
                        if p.stem < before_week)
    if not candidates:
        return None
    try:
        data = json.loads((history_dir / f"{candidates[-1]}.json").read_text())
        return RelativeStrengthSnapshot.from_dict(data)
    except Exception as exc:
        _log.warning("Could not load previous RS snapshot: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# RANKER
# ─────────────────────────────────────────────────────────────────────────────
class RelativeStrengthRanker:
    """
    Turns (ohlcv, sector_map, nifty_df, sector_snapshot) into a ranked
    RelativeStrengthSnapshot.

    Parameters
    ----------
    ohlcv           : {ticker: DataFrame}      (1y daily, from data_feed)
    sector_map      : {ticker: sector}         (from Universe.sector_map)
    nifty_df        : benchmark OHLCV          (fetch_nifty / get_market_regime)
    sector_snapshot : Phase-2 SectorSnapshot   (source of sector returns)
    """

    def __init__(self, ohlcv: dict[str, pd.DataFrame], sector_map: dict[str, str],
                 nifty_df: pd.DataFrame, sector_snapshot: SectorSnapshot,
                 benchmark: str = "^NSEI"):
        self.ohlcv = ohlcv
        self.sector_map = sector_map
        self.nifty_df = nifty_df
        self.sector_snapshot = sector_snapshot
        self.benchmark = benchmark

    def _sector_returns(self) -> dict[str, dict[str, Optional[float]]]:
        return {row.sector: {"r1m": row.r1m, "r3m": row.r3m, "r6m": row.r6m}
                for row in self.sector_snapshot.rows}

    def compute_metrics(self) -> list[RSMetrics]:
        nifty_returns = compute_stock_returns(self.nifty_df)
        calc = RelativeStrengthCalculator(nifty_returns, self._sector_returns())
        out = []
        for ticker, df in self.ohlcv.items():
            sector = self.sector_map.get(ticker, "DIVERSIFIED")
            out.append(calc.compute(ticker, sector, df))
        return out

    def rank(self, persist: bool = False) -> RelativeStrengthSnapshot:
        metrics = self.compute_metrics()
        wk = week_id_for()
        prev = load_previous_snapshot(wk)
        prev_ranks = {r.ticker: r.rank for r in prev.rows
                      if r.rank is not None} if prev else {}

        eligible = [m for m in metrics if m.eligible]
        ineligible = [m for m in metrics if not m.eligible]

        # Deterministic: composite RS desc, ticker asc.
        eligible.sort(key=lambda m: (-m.comp_rs, m.ticker))
        ineligible.sort(key=lambda m: m.ticker)
        M = len(eligible)

        pctl_nifty = _percentiles([(m.ticker, m.comp_rs_nifty) for m in eligible])
        pctl_sector = _percentiles([(m.ticker, m.comp_rs_sector) for m in eligible])

        rows: list[RSRow] = []
        for i, m in enumerate(eligible):
            rank = i + 1
            p = (rank - 1) / M if M > 1 else 0.0          # 0 best .. ~1 worst
            score = round(100 * (1 - p), 1)
            grade = self._grade(p)
            bucket = self._bucket(p)
            delta = (prev_ranks[m.ticker] - rank) if m.ticker in prev_ranks else None
            status, reasons = self._classify(
                m, pctl_nifty.get(m.ticker, 0.5), pctl_sector.get(m.ticker, 0.5),
                rank_pctl=p, rank_delta=delta,
            )
            rows.append(RSRow(
                m.symbol, m.ticker, m.sector, rank, score, grade, status, bucket, round(p, 4),
                m.comp_rs_nifty, m.comp_rs_sector,
                m.rs_nifty_1m, m.rs_nifty_3m, m.rs_nifty_6m,
                m.rs_sector_1m, m.rs_sector_3m, m.rs_sector_6m,
                delta, reasons,
            ))

        for m in ineligible:
            rows.append(RSRow(
                m.symbol, m.ticker, m.sector, None, 0.0, "C", "NEUTRAL", "NA", None,
                m.comp_rs_nifty, m.comp_rs_sector,
                m.rs_nifty_1m, m.rs_nifty_3m, m.rs_nifty_6m,
                m.rs_sector_1m, m.rs_sector_3m, m.rs_sector_6m,
                None, ["Insufficient history for RS"],
            ))

        # Benchmark availability drives the mode flags surfaced to the cockpit.
        bench_ok = compute_stock_returns(self.nifty_df).get(_MIN_ANCHOR) is not None
        snap = RelativeStrengthSnapshot(
            rows, datetime.now().isoformat(timespec="seconds"), wk, self.benchmark,
            rs_mode=("FULL" if bench_ok else "SECTOR_FALLBACK"),
            benchmark_status=("OK" if bench_ok else "MISSING"))
        if persist:
            snap.save()
        return snap

    # ── Grading / bucketing / classification ─────────────────────────────────
    @staticmethod
    def _grade(p: float) -> str:
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
    def _bucket(p: float) -> str:
        if p < 0.10:
            return "TOP_10"
        if p < 0.20:
            return "TOP_20"
        if p < 0.50:
            return "TOP_50"
        return "BOTTOM_50"

    @staticmethod
    def _classify(m: RSMetrics, pctl_nifty: float, pctl_sector: float,
                  rank_pctl: float, rank_delta: Optional[int]
                  ) -> tuple[str, list[str]]:
        """
        MARKET_LEADER  : top decile overall, beating Nifty AND sector
        SECTOR_LEADER  : strong vs sector, at least moderate vs Nifty
        EMERGING_LEADER: turning positive recently off a weak base / big rank rise
        LAGGARD        : negative RS vs both benchmarks
        NEUTRAL        : everything else
        """
        cn = m.comp_rs_nifty if m.comp_rs_nifty is not None else 0.0
        cs = m.comp_rs_sector if m.comp_rs_sector is not None else 0.0
        reasons: list[str] = []

        top_decile     = rank_pctl < _TOP_DECILE
        strong_nifty   = pctl_nifty >= _STRONG_PCTL
        strong_sector  = pctl_sector >= _STRONG_PCTL
        moderate_nifty = pctl_nifty >= _MODERATE_PCTL
        turning_up     = (m.rs_nifty_6m is not None and m.rs_nifty_6m <= 0
                          and m.rs_nifty_1m is not None and m.rs_nifty_1m > 0)
        rose_hard      = rank_delta is not None and rank_delta >= _EMERGING_DELTA

        if top_decile and cn > 0 and cs > 0:
            status = "MARKET_LEADER"
            reasons.append("Top-decile RS, beating Nifty & sector")
        elif strong_sector and (moderate_nifty or cn > 0):
            status = "SECTOR_LEADER"
            reasons.append("Outperforming sector peers")
        elif (turning_up or rose_hard) and not (cn < 0 and cs < 0):
            status = "EMERGING_LEADER"
            reasons.append("RS turning up off a weak base"
                           if turning_up else "Surging up the RS rankings")
        elif cn < 0 and cs < 0:
            status = "LAGGARD"
            reasons.append("Underperforming both Nifty and sector")
        else:
            status = "NEUTRAL"

        if m.comp_rs_nifty is not None:
            reasons.append(f"vs Nifty {m.comp_rs_nifty:+.1f}")
        if m.comp_rs_sector is not None:
            reasons.append(f"vs Sector {m.comp_rs_sector:+.1f}")
        if rank_delta:
            reasons.append(f"{'↑' if rank_delta > 0 else '↓'} {abs(rank_delta)} RS ranks WoW")
        return status, reasons
