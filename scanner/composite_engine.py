"""
scanner/composite_engine.py — Composite Ranking Engine (Phase 4)
================================================================
The decision core. Converts every available signal into ONE explainable score
and answers the only question that matters: "Why is Stock A ranked above Stock B?"

This is a weighted CONVICTION model, not an average. Proven alpha factors
dominate; supporting signals refine. Leadership (RS + Sector + Breakout = 65%)
drives the ranking.

    final = rs*0.25 + sector*0.20 + breakout*0.20
          + trend*0.15 + liquidity*0.10 + atr*0.05 + freshness*0.05

Phases 2 (sector) and 3 (RS) are CONSUMED, never rebuilt — their snapshots are
the source of truth via score_of()/rank_of()/bucket_of().

Scorers are independent: none knows about another. CompositeRanker orchestrates;
CompositeSnapshot stores and serves the ranked output (Phase 5 consumes it).

Every row carries a full component breakdown, a non-empty `drivers` list, and a
`warnings` list — a trader understands the ranking without reading source code.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from scanner.scanner import compute_atr_series
from scanner.sector_engine import SectorSnapshot, week_id_for
from scanner.relative_strength_engine import RelativeStrengthSnapshot

_log = logging.getLogger(__name__)

# ── Composite weight model (sums to 100, configurable) ───────────────────────
WEIGHTS = {
    "rs":        25,
    "sector":    20,
    "breakout":  20,
    "trend":     15,
    "liquidity": 10,
    "atr":        5,
    "freshness":  5,
}

# ── Hard-reject gates (LiquidityScorer) ──────────────────────────────────────
# Liquidity is gated on TURNOVER (₹ value traded = price × volume), not raw share
# count. In India a share-count floor wrongly rejects high-priced but extremely
# liquid leaders (MARUTI, ABB, APARINDS trade few shares but hundreds of ₹Cr/day).
MIN_PRICE         = 50
MIN_TURNOVER_CR   = 1.0           # ₹ Cr/day average — below this is genuinely illiquid
MIN_MARKET_CAP_CR = 2000          # enforced via Nifty-500 membership when cap data absent

# ── Grade thresholds ─────────────────────────────────────────────────────────
def grade_for(score: float) -> str:
    if score >= 90: return "A+"
    if score >= 80: return "A"
    if score >= 70: return "B+"
    if score >= 60: return "B"
    if score >= 50: return "C"
    return "D"

# ── RS classification → score mapping (Phase 3 is source of truth) ───────────
RS_STATUS_SCORE = {
    "MARKET_LEADER":   100,
    "SECTOR_LEADER":    90,
    "EMERGING_LEADER":  75,
    "NEUTRAL":          40,
    "LAGGARD":           0,
}

_HISTORY_DIR = Path("Journal/composite_history")
_HISTORY_CSV = Path("Journal/composite_history.csv")


# ═════════════════════════════════════════════════════════════════════════════
# INDEPENDENT SCORERS  (each returns a dict; none references another)
# ═════════════════════════════════════════════════════════════════════════════
class LiquidityScorer:
    """Hard-rejects garbage; grades survivors by tradability (volume)."""

    def score(self, df: pd.DataFrame, market_cap_cr: Optional[float] = None) -> dict:
        price = float(df["Close"].iloc[-1])
        avg_vol = float(df["Volume"].tail(20).mean()) if "Volume" in df.columns else 0.0
        turnover_cr = price * avg_vol / 1e7        # ₹ Cr/day (1 Cr = 1e7)

        rejected, reason = False, ""
        if price < MIN_PRICE:
            rejected, reason = True, f"price ₹{price:.0f} < ₹{MIN_PRICE}"
        elif turnover_cr < MIN_TURNOVER_CR:
            rejected, reason = True, f"turnover ₹{turnover_cr:.2f}Cr < ₹{MIN_TURNOVER_CR:.0f}Cr"
        elif market_cap_cr is not None and market_cap_cr < MIN_MARKET_CAP_CR:
            rejected, reason = True, f"mcap ₹{market_cap_cr:.0f}Cr < ₹{MIN_MARKET_CAP_CR}Cr"

        if rejected:
            return {"score": 0.0, "grade": "REJECT", "reason": reason, "rejected": True,
                    "avg_volume": avg_vol, "price": price, "turnover_cr": round(turnover_cr, 2)}

        # Graded by daily turnover — the professional tradability measure.
        if turnover_cr > 50:   s, lbl = 100, ">₹50Cr"
        elif turnover_cr > 25: s, lbl = 85, "₹25-50Cr"
        elif turnover_cr > 10: s, lbl = 70, "₹10-25Cr"
        elif turnover_cr > 5:  s, lbl = 55, "₹5-10Cr"
        else:                  s, lbl = 40, "₹1-5Cr"
        return {"score": float(s), "grade": "OK",
                "reason": f"turnover ₹{turnover_cr:.1f}Cr/day ({lbl})",
                "rejected": False, "avg_volume": avg_vol, "price": price,
                "turnover_cr": round(turnover_cr, 2)}


class TrendScorer:
    """Graded EMA-alignment quality. Downtrends rank low, never rejected."""

    def score(self, df: pd.DataFrame) -> dict:
        close = df["Close"]
        price = float(close.iloc[-1])
        e20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
        e50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
        e200_s = close.ewm(span=200, adjust=False).mean()
        e200 = float(e200_s.iloc[-1]) if len(close) >= 200 else float("nan")
        have200 = e200 == e200  # not NaN

        if have200 and price > e20 > e50 > e200:
            s, r = 100, "Price > EMA20 > EMA50 > EMA200 (full alignment)"
        elif price > e20 > e50:
            s, r = 80, "Price > EMA20 > EMA50 (uptrend)"
        elif e20 > e50 and price < e20:
            s, r = 60, "Pullback in uptrend (EMA20 > EMA50, price below EMA20)"
        elif abs(e20 - e50) / e50 < 0.005:
            s, r = 30, "EMA20 ≈ EMA50 (flat/transition)"
        elif e20 < e50:
            s, r = 0, "EMA20 < EMA50 (downtrend)"
        else:
            s, r = 50, "Mixed trend structure"
        return {"score": float(s), "reason": r,
                "ema20": round(e20, 2), "ema50": round(e50, 2),
                "ema200": round(e200, 2) if have200 else None}


class ATRScorer:
    """ATR as opportunity, never a filter. Moderate = good; extreme = penalised."""

    def score(self, df: pd.DataFrame) -> dict:
        atr = float(compute_atr_series(df).iloc[-1])
        close = float(df["Close"].iloc[-1])
        atr_pct = (atr / close * 100) if close else 0.0

        if atr_pct >= 12:   s, r = 20, f"ATR {atr_pct:.1f}% (too volatile/unstable)"
        elif atr_pct >= 8:  s, r = 50, f"ATR {atr_pct:.1f}% (elevated)"
        elif atr_pct >= 4:  s, r = 100, f"ATR {atr_pct:.1f}% (ideal swing range)"
        elif atr_pct >= 3:  s, r = 80, f"ATR {atr_pct:.1f}% (good)"
        elif atr_pct >= 2:  s, r = 60, f"ATR {atr_pct:.1f}% (moderate)"
        elif atr_pct >= 1:  s, r = 30, f"ATR {atr_pct:.1f}% (low)"
        else:               s, r = 0, f"ATR {atr_pct:.1f}% (dead)"
        return {"score": float(s), "reason": r, "atr_pct": round(atr_pct, 2)}


class BreakoutScorer:
    """40% consolidation tightness · 35% volume expansion · 25% breakout distance."""

    def score(self, df: pd.DataFrame) -> dict:
        high, low, close, vol = df["High"], df["Low"], df["Close"], df["Volume"]
        last = float(close.iloc[-1])

        # Resistance = highest high of the prior 20 bars (the breakout level).
        resistance = float(high.iloc[-21:-1].max()) if len(high) > 21 else float(high.max())

        # ── 1. Consolidation tightness × duration (40%) ──────────────────────
        win = close.tail(20)
        rng_pct = ((float(win.max()) - float(win.min())) / float(win.mean()) * 100) \
            if float(win.mean()) else 999
        if rng_pct < 8:    tight = 100
        elif rng_pct < 12: tight = 80
        elif rng_pct < 18: tight = 55
        elif rng_pct < 25: tight = 30
        else:              tight = 10
        base_days = self._base_length(high, resistance)
        if 10 <= base_days <= 30: dur = 1.0
        elif 5 <= base_days < 10: dur = 0.85
        elif 30 < base_days <= 45: dur = 0.90
        elif base_days < 5:       dur = 0.70
        else:                     dur = 0.80
        cons_q = tight * dur

        # ── 2. Volume expansion (35%) ────────────────────────────────────────
        avg20 = float(vol.tail(20).mean()) if "Volume" in df.columns else 0.0
        cur = float(vol.iloc[-1]) if "Volume" in df.columns else 0.0
        vexp = (cur / avg20) if avg20 else 1.0
        if vexp >= 2.5:   vscore = 100
        elif vexp >= 2.0: vscore = 90
        elif vexp >= 1.5: vscore = 75
        elif vexp >= 1.2: vscore = 55
        elif vexp >= 1.0: vscore = 40
        else:             vscore = 25

        # ── 3. Breakout distance (25%) ───────────────────────────────────────
        dscore, extension = self._distance_score(last, resistance)

        score = 0.40 * cons_q + 0.35 * vscore + 0.25 * dscore
        reason = (f"base {base_days}d/{rng_pct:.0f}% range, "
                  f"{vexp:.1f}x vol, {('+' if extension>=0 else '')}{extension:.1f}% vs breakout")
        return {"score": round(score, 1), "reason": reason,
                "breakout_level": round(resistance, 2),
                "volume_expansion": round(vexp, 2),
                "extension_pct": round(extension, 2),
                "base_days": base_days}

    @staticmethod
    def _base_length(high: pd.Series, resistance: float) -> int:
        """Trailing days the high stayed at/under resistance (capped at 60)."""
        days = 0
        for h in reversed(high.iloc[:-1].tolist()):
            if float(h) <= resistance * 1.001:
                days += 1
                if days >= 60:
                    break
            else:
                break
        return days

    @staticmethod
    def _distance_score(last: float, resistance: float) -> tuple[float, float]:
        if resistance <= 0:
            return 50.0, 0.0
        # extension > 0 means price is ABOVE the breakout level.
        extension = (last - resistance) / resistance * 100
        if extension < 0:                       # still below breakout (primed)
            below = -extension
            if below <= 2:   return 100.0, extension
            if below <= 4:   return 80.0, extension
            if below <= 6:   return 55.0, extension
            if below <= 10:  return 30.0, extension
            return 10.0, extension
        # just broke out vs extended/chasing
        if extension <= 3:   return 85.0, extension
        if extension <= 6:   return 50.0, extension
        return 20.0, extension


class FreshnessScorer:
    """Fresh breakouts outrank stale ones. Persists days_since_breakout."""

    def score(self, df: pd.DataFrame, lookback: int = 60) -> dict:
        high, close = df["High"], df["Close"]
        n = len(df)
        prior_high = high.shift(1).rolling(20).max()
        days_since = None
        start = max(1, n - lookback)
        for i in range(n - 1, start - 1, -1):
            ph = prior_high.iloc[i]
            if ph == ph and float(close.iloc[i]) > float(ph):   # breakout day
                days_since = (n - 1) - i
                break

        if days_since is None:
            return {"score": 50.0, "days_since_breakout": None,
                    "reason": "no recent breakout (base building)"}
        if days_since <= 2:   s = 100
        elif days_since <= 5: s = 70
        elif days_since <= 10: s = 40
        else:                 s = 0
        return {"score": float(s), "days_since_breakout": days_since,
                "reason": f"{days_since}d since breakout"}


# ═════════════════════════════════════════════════════════════════════════════
# RANKED OUTPUT
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class CompositeRow:
    ticker:              str
    symbol:              str
    sector:              str
    rank:                int
    score:               float
    grade:               str
    components:          dict[str, float]
    drivers:             list[str]
    warnings:            list[str]
    rs_status:           str
    rs_bucket:           str
    sector_rank:         Optional[int]
    days_since_breakout: Optional[int]
    breakout_level:      float
    volume_expansion:    float
    price:               float


@dataclass
class CompositeSnapshot:
    rows:         list[CompositeRow]
    generated_at: str
    week_id:      str
    rejected:     list[dict] = field(default_factory=list)
    weights:      dict[str, int] = field(default_factory=lambda: dict(WEIGHTS))

    # ── Phase-5 consumption APIs ─────────────────────────────────────────────
    def top_n(self, n: int = 20) -> list[CompositeRow]:
        return self.rows[:n]

    def grade_distribution(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[r.grade] = out.get(r.grade, 0) + 1
        return {g: out.get(g, 0) for g in ("A+", "A", "B+", "B", "C", "D")}

    def component_breakdown(self, ticker: str) -> Optional[dict]:
        r = self.get(ticker)
        return None if r is None else {
            "ticker": r.ticker, "score": r.score, "grade": r.grade,
            "components": r.components, "drivers": r.drivers, "warnings": r.warnings,
        }

    def sector_distribution(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[r.sector] = out.get(r.sector, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def get(self, ticker: str) -> Optional[CompositeRow]:
        return next((r for r in self.rows if r.ticker == ticker), None)

    # ── Serialisation / persistence ──────────────────────────────────────────
    def to_dict(self) -> dict:
        return {"generated_at": self.generated_at, "week_id": self.week_id,
                "weights": self.weights, "rejected": self.rejected,
                "rows": [asdict(r) for r in self.rows]}

    def export_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict) -> "CompositeSnapshot":
        rows = [CompositeRow(**r) for r in d.get("rows", [])]
        return cls(rows, d.get("generated_at", ""), d.get("week_id", ""),
                   d.get("rejected", []), d.get("weights", dict(WEIGHTS)))

    def save(self, history_dir: Path = _HISTORY_DIR,
             history_csv: Path = _HISTORY_CSV) -> Path:
        history_dir.mkdir(parents=True, exist_ok=True)
        path = history_dir / f"{self.week_id}.json"
        path.write_text(self.export_json())
        self._append_csv(history_csv)
        return path

    def _append_csv(self, history_csv: Path) -> None:
        try:
            new = pd.DataFrame([{
                "week_id": self.week_id, "date": self.generated_at[:10],
                "rank": r.rank, "ticker": r.ticker, "sector": r.sector,
                "score": r.score, "grade": r.grade, "rs_status": r.rs_status,
                "rs_bucket": r.rs_bucket,
                "drivers": " | ".join(r.drivers),
            } for r in self.rows])
            if history_csv.exists():
                old = pd.read_csv(history_csv)
                old = old[old["week_id"] != self.week_id]
                new = pd.concat([old, new], ignore_index=True)
            history_csv.parent.mkdir(parents=True, exist_ok=True)
            new.to_csv(history_csv, index=False, encoding="utf-8")
        except Exception as exc:
            _log.warning("Could not append composite history CSV: %s", exc)


# ═════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR
# ═════════════════════════════════════════════════════════════════════════════
class CompositeRanker:
    """
    Orchestrates the five scorers + consumes Phase-2/3 snapshots into one
    explainable ranked board.

    Parameters
    ----------
    ohlcv           : {ticker: DataFrame}        (1y daily)
    sector_map      : {ticker: sector}
    sector_snapshot : Phase-2 SectorSnapshot     (sector score/rank source of truth)
    rs_snapshot     : Phase-3 RelativeStrengthSnapshot
    weights         : override the composite weights (optional)
    market_caps     : optional {ticker: cr} to enforce the explicit cap gate
    """

    def __init__(self, ohlcv: dict[str, pd.DataFrame], sector_map: dict[str, str],
                 sector_snapshot: SectorSnapshot, rs_snapshot: RelativeStrengthSnapshot,
                 weights: Optional[dict[str, int]] = None,
                 market_caps: Optional[dict[str, float]] = None):
        self.ohlcv = ohlcv
        self.sector_map = sector_map
        self.sector_snapshot = sector_snapshot
        self.rs_snapshot = rs_snapshot
        self.weights = weights or dict(WEIGHTS)
        self.market_caps = market_caps or {}
        self.liquidity = LiquidityScorer()
        self.trend = TrendScorer()
        self.atr = ATRScorer()
        self.breakout = BreakoutScorer()
        self.freshness = FreshnessScorer()

    def rank(self, persist: bool = False) -> CompositeSnapshot:
        rows: list[CompositeRow] = []
        rejected: list[dict] = []

        for ticker, df in self.ohlcv.items():
            if df is None or df.empty or len(df) < 30:
                rejected.append({"ticker": ticker, "reason": "insufficient bars"})
                continue

            liq = self.liquidity.score(df, self.market_caps.get(ticker))
            if liq["rejected"]:
                rejected.append({"ticker": ticker, "reason": liq["reason"]})
                continue

            sector = self.sector_map.get(ticker, "DIVERSIFIED")
            trd = self.trend.score(df)
            atrd = self.atr.score(df)
            brk = self.breakout.score(df)
            frsh = self.freshness.score(df)

            rs_row = self.rs_snapshot.get(ticker)
            rs_status = rs_row.status if rs_row else "NEUTRAL"
            rs_bucket = rs_row.bucket if rs_row else "NA"
            rs_score = float(RS_STATUS_SCORE.get(rs_status, 40))
            sector_score = float(self.sector_snapshot.score_of(sector, 50.0))
            sector_rank = self.sector_snapshot.rank_of(sector)

            components = {
                "rs": round(rs_score, 1),
                "sector": round(sector_score, 1),
                "breakout": round(brk["score"], 1),
                "trend": round(trd["score"], 1),
                "liquidity": round(liq["score"], 1),
                "atr": round(atrd["score"], 1),
                "freshness": round(frsh["score"], 1),
            }
            final = round(sum(components[k] * self.weights[k]
                              for k in self.weights) / sum(self.weights.values()), 2)

            drivers, warnings = self._explain(
                components, rs_status, rs_bucket, sector, sector_rank,
                brk, atrd, trd, liq, frsh)

            rows.append(CompositeRow(
                ticker, ticker[:-3] if ticker.endswith(".NS") else ticker, sector,
                0, final, grade_for(final), components, drivers, warnings,
                rs_status, rs_bucket, sector_rank,
                frsh["days_since_breakout"], brk["breakout_level"],
                brk["volume_expansion"], liq["price"],
            ))

        # Deterministic: score desc, ticker asc.
        rows.sort(key=lambda r: (-r.score, r.ticker))
        for i, r in enumerate(rows, 1):
            r.rank = i

        snap = CompositeSnapshot(rows, datetime.now().isoformat(timespec="seconds"),
                                 week_id_for(), rejected, dict(self.weights))
        if persist:
            snap.save()
        return snap

    # ── Explainability ───────────────────────────────────────────────────────
    @staticmethod
    def _explain(comp, rs_status, rs_bucket, sector, sector_rank,
                 brk, atrd, trd, liq, frsh) -> tuple[list[str], list[str]]:
        drivers: list[str] = []
        warnings: list[str] = []

        # Drivers — what's pulling this stock UP.
        if rs_bucket == "TOP_10":
            drivers.append("Top 10% Relative Strength")
        elif rs_bucket == "TOP_20":
            drivers.append("Top 20% Relative Strength")
        if rs_status in ("MARKET_LEADER", "SECTOR_LEADER"):
            drivers.append(rs_status.replace("_", " ").title())
        if sector_rank is not None and sector_rank <= 5:
            drivers.append(f"Sector #{sector_rank} ({sector})")
        if frsh["days_since_breakout"] is not None and frsh["days_since_breakout"] <= 2:
            drivers.append("Fresh breakout (≤2d)")
        if brk["volume_expansion"] >= 1.5:
            drivers.append(f"{brk['volume_expansion']:.1f}x volume expansion")
        if comp["trend"] >= 80:
            drivers.append("Strong uptrend")
        if comp["breakout"] >= 75 and "Fresh breakout (≤2d)" not in drivers:
            drivers.append("High-quality base")
        if comp["atr"] == 100:
            drivers.append("Ideal volatility")
        # Guarantee non-empty: fall back to the single best component.
        if not drivers:
            best = max(comp, key=comp.get)
            drivers.append(f"Best factor: {best} {comp[best]:.0f}")

        # Warnings — what a trader should watch.
        if atrd["atr_pct"] >= 8:
            warnings.append("ATR elevated")
        if brk["extension_pct"] > 5:
            warnings.append("Extended above breakout")
        if comp["trend"] == 0:
            warnings.append("Downtrend")
        if liq["score"] <= 40:
            warnings.append("Thin liquidity")
        if frsh["days_since_breakout"] is not None and frsh["days_since_breakout"] > 10:
            warnings.append("Stale breakout")
        if rs_status == "LAGGARD":
            warnings.append("RS laggard")
        return drivers, warnings
