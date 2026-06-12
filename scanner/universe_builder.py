"""
scanner/universe_builder.py — Dynamic NSE Universe Builder
==========================================================
Builds the tradable swing-trading universe from the live Nifty 500
constituent list published by NSE.

The Nifty 500 file is the single source of three things the screener needs:
  • universe membership   (which stocks to scan)
  • sector classification  (NSE "Industry" column → Sector Leadership Engine)
  • market-cap proxy       (membership ≈ large/mid-cap, no per-stock scraping)

Resilience ladder — the screener must NEVER fail because NSE is unreachable:

  1. Fresh cache (< TTL days)      → use it, no network call
  2. Download from NSE             → validate schema + count, cache, use
  3. Stale cache (any age)         → use it, log a warning
  4. Bundled emergency universe    → use it, log a warning
                                      (data/emergency_universe.csv)

Canonical schema (the DataFrame every consumer receives):
    symbol   e.g. "RELIANCE"
    ticker   e.g. "RELIANCE.NS"   (yfinance / Kite suffix)
    sector   e.g. "Financial Services"
    name     e.g. "Reliance Industries Ltd."
"""

from __future__ import annotations

import io
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

_log = logging.getLogger(__name__)

# ── Paths & constants ────────────────────────────────────────────────────────
_CACHE_DIR      = Path("cache/universe")
_CACHE_CSV      = _CACHE_DIR / "nifty500.csv"
_META_JSON      = _CACHE_DIR / "meta.json"
_EMERGENCY_FILE = Path("data/emergency_universe.csv")

_CACHE_TTL_DAYS = 7
_MIN_SYMBOLS    = 400          # a healthy Nifty 500 file holds ~500 rows
_DOWNLOAD_RETRIES = 3
_HTTP_TIMEOUT   = 15           # seconds

# NSE serves the constituent CSV from these mirrors (tried in order).
_NSE_URLS = [
    "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://www1.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
]
_NSE_HOME = "https://www.nseindia.com"
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/csv,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}


# ── Public result type ───────────────────────────────────────────────────────
@dataclass
class Universe:
    """The resolved universe plus provenance for explainability/logging."""
    df:           pd.DataFrame      # canonical schema: symbol, ticker, sector, name
    source:       str               # "nse_live" | "cache_fresh" | "cache_stale" | "emergency"
    generated_at: str               # ISO timestamp of when the data was obtained
    age_days:     float             # age of the underlying data in days
    warnings:     list[str]

    @property
    def tickers(self) -> list[str]:
        return self.df["ticker"].tolist()

    @property
    def symbol_count(self) -> int:
        return len(self.df)

    @property
    def sector_map(self) -> dict[str, str]:
        """{ticker: sector} for every member — replaces the hardcoded map."""
        return dict(zip(self.df["ticker"], self.df["sector"]))


# ─────────────────────────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
def build_universe(force_refresh: bool = False,
                   ttl_days: int = _CACHE_TTL_DAYS) -> Universe:
    """
    Resolve the tradable universe, walking the resilience ladder.

    force_refresh=True skips the fresh-cache shortcut and forces a download
    attempt (still falls back gracefully if NSE is unreachable).
    """
    warnings: list[str] = []

    # ── Rung 1: fresh cache ──────────────────────────────────────────────────
    if not force_refresh:
        cached = _load_cache()
        if cached is not None:
            df, meta = cached
            age = _age_days(meta.get("generated_at"))
            if age is not None and age < ttl_days:
                return Universe(df, "cache_fresh", meta.get("generated_at", ""),
                                round(age, 2), warnings)

    # ── Rung 2: download from NSE ────────────────────────────────────────────
    df = _download_nifty500()
    if df is not None:
        ok, reason = _validate(df)
        if ok:
            now = datetime.now().isoformat(timespec="seconds")
            _save_cache(df, source="nse_live", generated_at=now)
            return Universe(df, "nse_live", now, 0.0, warnings)
        warnings.append(f"NSE download failed validation ({reason}); falling back to cache.")
        _log.warning(warnings[-1])
    else:
        warnings.append("NSE download unavailable; falling back to cache.")
        _log.warning(warnings[-1])

    # ── Rung 3: stale cache ──────────────────────────────────────────────────
    cached = _load_cache()
    if cached is not None:
        df, meta = cached
        age = _age_days(meta.get("generated_at")) or 999.0
        warnings.append(f"Using stale universe cache ({age:.0f} days old).")
        _log.warning(warnings[-1])
        return Universe(df, "cache_stale", meta.get("generated_at", ""),
                        round(age, 2), warnings)

    # ── Rung 4: bundled emergency universe ───────────────────────────────────
    df = _load_emergency()
    warnings.append(
        f"No NSE access and no cache — using bundled emergency universe "
        f"({len(df)} symbols). Run with network access to refresh."
    )
    _log.warning(warnings[-1])
    return Universe(df, "emergency", "bundled", 999.0, warnings)


# ─────────────────────────────────────────────────────────────────────────────
# DOWNLOAD
# ─────────────────────────────────────────────────────────────────────────────
def _download_nifty500() -> Optional[pd.DataFrame]:
    """
    Fetch and parse the Nifty 500 constituent CSV into canonical schema.
    Returns None on any network/parse failure (never raises).
    """
    try:
        import requests
    except ImportError:
        _log.warning("requests not installed — cannot download universe.")
        return None

    session = requests.Session()
    session.headers.update(_BROWSER_HEADERS)
    # Warm up cookies: NSE rejects cold requests without a homepage visit first.
    try:
        session.get(_NSE_HOME, timeout=_HTTP_TIMEOUT)
    except Exception:
        pass  # cookie warm-up is best-effort

    for url in _NSE_URLS:
        for attempt in range(1, _DOWNLOAD_RETRIES + 1):
            try:
                resp = session.get(url, timeout=_HTTP_TIMEOUT)
                if resp.status_code == 200 and resp.text.strip():
                    df = _parse_nse_csv(resp.text)
                    if df is not None and not df.empty:
                        _log.info("Downloaded Nifty 500 from %s (%d rows)", url, len(df))
                        return df
                else:
                    _log.debug("NSE %s → HTTP %s (attempt %d)", url,
                               resp.status_code, attempt)
            except Exception as exc:
                _log.debug("NSE download error %s (attempt %d): %s", url, attempt, exc)
            time.sleep(1.5 * attempt)  # linear backoff
    return None


def _parse_nse_csv(text: str) -> Optional[pd.DataFrame]:
    """Parse NSE constituent CSV text → canonical schema, or None."""
    try:
        raw = pd.read_csv(io.StringIO(text))
    except Exception as exc:
        _log.debug("CSV parse failed: %s", exc)
        return None

    # NSE column headers occasionally carry stray whitespace.
    raw.columns = [str(c).strip() for c in raw.columns]
    colmap = {c.lower(): c for c in raw.columns}

    sym_col = colmap.get("symbol")
    if sym_col is None:
        return None
    ind_col  = colmap.get("industry")
    name_col = colmap.get("company name")

    out = pd.DataFrame()
    out["symbol"] = raw[sym_col].astype(str).str.strip().str.upper()
    out["sector"] = (raw[ind_col].astype(str).str.strip()
                     if ind_col else "DIVERSIFIED")
    out["name"]   = (raw[name_col].astype(str).str.strip()
                     if name_col else out["symbol"])
    return _finalize(out)


# ─────────────────────────────────────────────────────────────────────────────
# VALIDATION
# ─────────────────────────────────────────────────────────────────────────────
def _validate(df: pd.DataFrame) -> tuple[bool, str]:
    """Schema + sanity checks before trusting a freshly downloaded universe."""
    required = {"symbol", "ticker", "sector", "name"}
    missing = required - set(df.columns)
    if missing:
        return False, f"missing columns {missing}"
    if len(df) < _MIN_SYMBOLS:
        return False, f"only {len(df)} symbols (< {_MIN_SYMBOLS})"
    if df["symbol"].isna().any() or (df["symbol"] == "").any():
        return False, "blank symbols present"
    if df["symbol"].duplicated().any():
        return False, "duplicate symbols present"
    return True, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# CACHE I/O
# ─────────────────────────────────────────────────────────────────────────────
def _save_cache(df: pd.DataFrame, source: str, generated_at: str) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(_CACHE_CSV, index=False, encoding="utf-8")
        _META_JSON.write_text(json.dumps({
            "source":        source,
            "generated_at":  generated_at,
            "symbol_count":  len(df),
        }, indent=2))
    except Exception as exc:
        _log.warning("Could not write universe cache: %s", exc)


def _load_cache() -> Optional[tuple[pd.DataFrame, dict]]:
    if not _CACHE_CSV.exists():
        return None
    try:
        df = _finalize(pd.read_csv(_CACHE_CSV, dtype=str))
        meta = (json.loads(_META_JSON.read_text())
                if _META_JSON.exists() else {})
        # Backfill generated_at from file mtime if meta is missing.
        if "generated_at" not in meta:
            ts = datetime.fromtimestamp(_CACHE_CSV.stat().st_mtime)
            meta["generated_at"] = ts.isoformat(timespec="seconds")
        return df, meta
    except Exception as exc:
        _log.warning("Could not read universe cache: %s", exc)
        return None


def _load_emergency() -> pd.DataFrame:
    """Bundled fallback — guaranteed to exist in the repo."""
    if _EMERGENCY_FILE.exists():
        try:
            return _finalize(pd.read_csv(_EMERGENCY_FILE, dtype=str))
        except Exception as exc:
            _log.warning("Emergency universe unreadable: %s", exc)
    # Last-ditch in-code fallback so screening can still run.
    return _finalize(pd.DataFrame({
        "symbol": ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS"],
        "sector": ["DIVERSIFIED"] * 5,
        "name":   ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS"],
    }))


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def _finalize(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise to canonical schema: clean symbol, derive ticker, dedupe."""
    df = df.copy()
    df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
    df = df[df["symbol"].ne("") & df["symbol"].notna()]
    # NSE seeds the constituent file with placeholder rows during corporate
    # actions (e.g. "DUMMYVEDL1" for the Vedanta demerger) — not tradable.
    df = df[~df["symbol"].str.contains("DUMMY", na=False)]
    df = df.drop_duplicates(subset="symbol")
    if "sector" not in df.columns:
        df["sector"] = "DIVERSIFIED"
    if "name" not in df.columns:
        df["name"] = df["symbol"]
    df["sector"] = df["sector"].fillna("DIVERSIFIED").replace("", "DIVERSIFIED")
    df["name"]   = df["name"].fillna(df["symbol"])
    df["ticker"] = df["symbol"] + ".NS"
    return df[["symbol", "ticker", "sector", "name"]].reset_index(drop=True)


def _age_days(generated_at: Optional[str]) -> Optional[float]:
    if not generated_at:
        return None
    try:
        then = datetime.fromisoformat(generated_at)
        return (datetime.now() - then).total_seconds() / 86400.0
    except Exception:
        return None
