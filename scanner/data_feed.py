"""
scanner/data_feed.py — Bulk Cached OHLCV Feed
=============================================
One interface, swappable backend (yfinance today, Kite tomorrow).

Responsibilities the per-ticker `scanner.scanner.fetch_stock_data` cannot meet
at universe scale (~500 stocks):
  • BULK download in batches (not 500 sequential round-trips)
  • DAY CACHE  — first run downloads, same-day re-runs read the cache in seconds
  • RETRY      — failed tickers get a second individual attempt
  • VALIDATE   — every frame is cleaned + integrity-checked (reuses data_validator)
  • REPORT     — coverage %, failed list, timing, so Phase-1 verification is trivial

The backend is isolated behind `OHLCVBackend`. To switch to Kite later, implement
`KiteBackend.download()` and pass it to `get_ohlcv(backend=...)`; nothing else changes.

Cache layout (CSV — pyarrow/parquet not assumed):
    cache/ohlcv/<YYYY-MM-DD>_<period>.csv
    columns: ticker, Date, Open, High, Low, Close, Volume
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd

from scanner.data_validator import clean_ohlcv, validate_ohlcv

_log = logging.getLogger(__name__)

_CACHE_DIR = Path("cache/ohlcv")
_OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]


# ─────────────────────────────────────────────────────────────────────────────
# RESULT TYPE
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class FetchResult:
    """Outcome of a bulk fetch — carries data plus coverage diagnostics."""
    data:       dict[str, pd.DataFrame]    # {ticker: clean OHLCV frame}
    requested:  int
    failed:     list[str] = field(default_factory=list)
    source:     str = ""                    # "cache" | "download"
    elapsed_s:  float = 0.0

    @property
    def succeeded(self) -> int:
        return len(self.data)

    @property
    def coverage(self) -> float:
        return self.succeeded / self.requested if self.requested else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# BACKEND INTERFACE
# ─────────────────────────────────────────────────────────────────────────────
class OHLCVBackend:
    """A data source that returns raw {ticker: DataFrame} for a ticker list."""
    name = "base"

    def download(self, tickers: list[str], period: str) -> dict[str, pd.DataFrame]:
        raise NotImplementedError


class YFinanceBackend(OHLCVBackend):
    """
    Batched yfinance backend. Downloads in chunks, splits the wide multi-index
    frame into per-ticker frames, and retries the failures once individually.
    """
    name = "yfinance"

    def __init__(self, batch_size: int = 100, retries: int = 1,
                 pause_between_batches: float = 0.5):
        self.batch_size = batch_size
        self.retries = retries
        self.pause = pause_between_batches

    def download(self, tickers: list[str], period: str) -> dict[str, pd.DataFrame]:
        import yfinance as yf

        out: dict[str, pd.DataFrame] = {}
        pending = list(dict.fromkeys(tickers))  # de-dupe, preserve order

        for attempt in range(self.retries + 1):
            if not pending:
                break
            still_missing: list[str] = []
            for batch in _chunks(pending, self.batch_size):
                frames = self._download_batch(yf, batch, period)
                for t in batch:
                    df = frames.get(t)
                    if df is not None and not df.empty:
                        out[t] = df
                    else:
                        still_missing.append(t)
                if self.pause:
                    time.sleep(self.pause)
            pending = still_missing
            if pending and attempt < self.retries:
                _log.info("yfinance retry %d for %d ticker(s)", attempt + 1, len(pending))

        return out

    @staticmethod
    def _download_batch(yf, batch: list[str], period: str) -> dict[str, pd.DataFrame]:
        try:
            raw = yf.download(
                batch, period=period, interval="1d", auto_adjust=True,
                group_by="ticker", threads=True, progress=False,
            )
        except Exception as exc:
            _log.debug("batch download failed (%d tickers): %s", len(batch), exc)
            return {}

        if raw is None or raw.empty:
            return {}

        result: dict[str, pd.DataFrame] = {}
        # Single ticker → flat columns; multiple → MultiIndex (ticker, field).
        if isinstance(raw.columns, pd.MultiIndex):
            for t in batch:
                if t in raw.columns.get_level_values(0):
                    sub = raw[t].dropna(how="all")
                    if not sub.empty:
                        result[t] = sub
        elif len(batch) == 1:
            sub = raw.dropna(how="all")
            if not sub.empty:
                result[batch[0]] = sub
        return result


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
def get_ohlcv(tickers: list[str], period: str = "6mo",
              force_refresh: bool = False, min_bars: int = 60,
              backend: Optional[OHLCVBackend] = None,
              validate: bool = True) -> FetchResult:
    """
    Return cleaned daily OHLCV for *tickers*, using a same-day cache when present.

    Parameters
    ----------
    period        yfinance period string ("6mo", "1y", ...)
    force_refresh ignore today's cache and re-download
    min_bars      drop tickers with fewer than this many bars
    backend       OHLCVBackend to use (defaults to YFinanceBackend)
    validate      run data_validator integrity checks (drops corrupt frames)
    """
    t0 = time.time()
    tickers = list(dict.fromkeys(t.strip().upper() for t in tickers if t and t.strip()))

    # ── Cache hit ────────────────────────────────────────────────────────────
    if not force_refresh:
        cached = _load_cache(period)
        if cached is not None:
            data = _postprocess(cached, tickers, min_bars, validate)
            failed = [t for t in tickers if t not in data]
            return FetchResult(data, len(tickers), failed, "cache",
                               round(time.time() - t0, 2))

    # ── Download ─────────────────────────────────────────────────────────────
    backend = backend or YFinanceBackend()
    raw = backend.download(tickers, period)
    data = _postprocess(raw, tickers, min_bars, validate)
    failed = [t for t in tickers if t not in data]

    if data:
        _save_cache(data, period)

    return FetchResult(data, len(tickers), failed, "download",
                       round(time.time() - t0, 2))


# ─────────────────────────────────────────────────────────────────────────────
# POST-PROCESSING (clean + validate)
# ─────────────────────────────────────────────────────────────────────────────
def _postprocess(raw: dict[str, pd.DataFrame], tickers: list[str],
                 min_bars: int, validate: bool) -> dict[str, pd.DataFrame]:
    want = set(tickers)
    out: dict[str, pd.DataFrame] = {}
    for t, df in raw.items():
        if t not in want or df is None or df.empty:
            continue
        df = df[[c for c in _OHLCV_COLS if c in df.columns]].copy()
        if not {"Open", "High", "Low", "Close"}.issubset(df.columns):
            continue
        df = clean_ohlcv(df).dropna()
        if len(df) < min_bars:
            continue
        if validate and not validate_ohlcv(df, ticker=t).valid:
            continue
        out[t] = df
    return out


# ─────────────────────────────────────────────────────────────────────────────
# CACHE I/O
# ─────────────────────────────────────────────────────────────────────────────
def _cache_path(period: str) -> Path:
    return _CACHE_DIR / f"{date.today().isoformat()}_{period}.csv"


def _save_cache(data: dict[str, pd.DataFrame], period: str) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        frames = []
        for t, df in data.items():
            f = df.copy()
            f.index.name = "Date"
            f = f.reset_index()        # Date becomes a column
            f.insert(0, "ticker", t)   # → [ticker, Date, Open, High, Low, Close, Volume]
            frames.append(f)
        if frames:
            pd.concat(frames, ignore_index=True).to_csv(
                _cache_path(period), index=False, encoding="utf-8")
    except Exception as exc:
        _log.warning("Could not write OHLCV cache: %s", exc)


def _load_cache(period: str) -> Optional[dict[str, pd.DataFrame]]:
    path = _cache_path(period)
    if not path.exists():
        return None
    try:
        big = pd.read_csv(path, parse_dates=["Date"])
    except Exception as exc:
        _log.warning("Could not read OHLCV cache: %s", exc)
        return None

    out: dict[str, pd.DataFrame] = {}
    for t, grp in big.groupby("ticker"):
        g = grp.drop(columns=["ticker"]).set_index("Date").sort_index()
        out[str(t)] = g
    return out


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _chunks(seq: list[str], size: int):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]
