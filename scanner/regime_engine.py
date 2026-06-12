"""
scanner/regime_engine.py — Market Regime Engine (Phase 6)
=========================================================
Answers, continuously: "what KIND of market are we trading in right now?"

A breakout playbook that prints money in a strong bull can bleed in a range or
a volatility shock. Every adaptive behaviour in later phases keys off this layer.

This is NOT "Nifty > EMA200 ⇒ bull". Regime is read across five independent
dimensions and fused into a score + a structural label:

    Trend 30 · Breadth 25 · Leadership 20 · Participation 15 · Volatility 10

    STRONG_BULL · BULL · NEUTRAL · RANGE · WEAK_BEAR · STRONG_BEAR · VOLATILE

Analyzers are independent (none references another). RegimeClassifier is the
orchestrator. Phases 2/3/4/5 snapshots are CONSUMED, never rebuilt.

Classes
-------
    TrendRegimeAnalyzer        Nifty 50 + Nifty 500 EMA structure
    BreadthAnalyzer            % of universe above EMA20/50/200
    VolatilityAnalyzer         Nifty ATR% / India VIX → calm..volatile
    SectorParticipationAnalyzer  breadth of sector leadership (Phase 2)
    LeadershipAnalyzer         leader/A-grade/ACTION_NOW counts (Phase 3/4/5)
    RegimeClassifier           orchestrates → RegimeSnapshot
    RegimeSnapshot             stored + persisted regime read
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
from scanner.sector_engine import SectorSnapshot, week_id_for
from scanner.relative_strength_engine import RelativeStrengthSnapshot
from scanner.composite_engine import CompositeSnapshot

_log = logging.getLogger(__name__)

# ── Composite regime weights (sum 100) ───────────────────────────────────────
REGIME_WEIGHTS = {"trend": 0.30, "breadth": 0.25, "leadership": 0.20,
                  "participation": 0.15, "volatility": 0.10}

_HISTORY_DIR = Path("Journal/regime_history")
_HISTORY_CSV = Path("Journal/regime_history.csv")


def _ema(series: pd.Series, span: int) -> float:
    return float(series.ewm(span=span, adjust=False).mean().iloc[-1])


# ═════════════════════════════════════════════════════════════════════════════
# COMPONENT 1 — TREND
# ═════════════════════════════════════════════════════════════════════════════
class TrendRegimeAnalyzer:
    """EMA structure of the broad indices (Nifty 50 + Nifty 500)."""

    def analyze(self, index_dfs: dict[str, pd.DataFrame]) -> dict:
        scores, details = [], {}
        for name, df in index_dfs.items():
            if df is None or len(df) < 50:
                continue
            s, detail = self._one(df)
            scores.append(s)
            details[name] = detail
        if not scores:
            return {"score": 50.0, "state": "NEUTRAL",
                    "reason": "no index data", "detail": {}}
        score = round(sum(scores) / len(scores), 1)
        state = ("STRONG_UP" if score >= 80 else "UP" if score >= 55
                 else "NEUTRAL" if score >= 40 else "DOWN")
        idxs = ", ".join(f"{k} {v['state']}" for k, v in details.items())
        return {"score": score, "state": state,
                "reason": f"Index trend: {idxs}", "detail": details}

    @staticmethod
    def _one(df: pd.DataFrame) -> tuple[float, dict]:
        close = df["Close"]
        price = float(close.iloc[-1])
        e50, e200 = _ema(close, 50), _ema(close, 200) if len(close) >= 200 else _ema(close, 100)
        above50, above200, golden = price > e50, price > e200, e50 > e200
        score = 40 * above200 + 30 * above50 + 30 * golden
        state = ("STRONG_UP" if score >= 80 else "UP" if score >= 55
                 else "NEUTRAL" if score >= 40 else "DOWN")
        return float(score), {"state": state, "above_ema50": above50,
                              "above_ema200": above200, "golden_cross": golden}


# ═════════════════════════════════════════════════════════════════════════════
# COMPONENT 2 — BREADTH
# ═════════════════════════════════════════════════════════════════════════════
class BreadthAnalyzer:
    """% of the universe trading above EMA20 / EMA50 / EMA200."""

    def analyze(self, ohlcv: dict[str, pd.DataFrame]) -> dict:
        n20 = d20 = n50 = d50 = n200 = d200 = 0
        for df in ohlcv.values():
            if df is None or df.empty or "Close" not in df.columns:
                continue
            close = df["Close"]
            price = float(close.iloc[-1])
            if len(close) >= 20:
                d20 += 1; n20 += price > _ema(close, 20)
            if len(close) >= 50:
                d50 += 1; n50 += price > _ema(close, 50)
            if len(close) >= 200:
                d200 += 1; n200 += price > _ema(close, 200)

        p20 = round(100 * n20 / d20, 1) if d20 else 0.0
        p50 = round(100 * n50 / d50, 1) if d50 else 0.0
        p200 = round(100 * n200 / d200, 1) if d200 else 0.0
        score = round(0.30 * p20 + 0.40 * p50 + 0.30 * p200, 1)
        return {"score": score,
                "metrics": {"above_ema20": p20, "above_ema50": p50,
                            "above_ema200": p200, "sample": d50},
                "reason": f"{p20:.0f}% > EMA20, {p50:.0f}% > EMA50, {p200:.0f}% > EMA200"}


# ═════════════════════════════════════════════════════════════════════════════
# COMPONENT 3 — VOLATILITY
# ═════════════════════════════════════════════════════════════════════════════
class VolatilityAnalyzer:
    """Nifty ATR% (and India VIX if available) → calm/normal/elevated/volatile.

    Middle regimes score best; extreme volatility collapses confidence and is
    flagged is_extreme so the classifier can override the label to VOLATILE.
    """

    def analyze(self, nifty_df: Optional[pd.DataFrame],
                vix_df: Optional[pd.DataFrame] = None) -> dict:
        atr_pct, vix = None, None
        if nifty_df is not None and len(nifty_df) > 20:
            atr = float(compute_atr_series(nifty_df).iloc[-1])
            close = float(nifty_df["Close"].iloc[-1])
            atr_pct = round(atr / close * 100, 2) if close else None
        if vix_df is not None and not vix_df.empty:
            vix = round(float(vix_df["Close"].iloc[-1]), 1)

        # Prefer VIX when present; else absolute Nifty ATR% thresholds.
        if vix is not None:
            state = ("CALM" if vix < 12 else "NORMAL" if vix < 16
                     else "ELEVATED" if vix < 22 else "VOLATILE")
            basis = f"India VIX {vix}"
        elif atr_pct is not None:
            state = ("CALM" if atr_pct < 0.8 else "NORMAL" if atr_pct < 1.6
                     else "ELEVATED" if atr_pct < 3.0 else "VOLATILE")
            basis = f"Nifty ATR {atr_pct}%"
        else:
            return {"state": "NORMAL", "score": 60.0, "is_extreme": False,
                    "reason": "no volatility data", "atr_pct": None, "vix": None}

        score = {"NORMAL": 100.0, "CALM": 80.0, "ELEVATED": 45.0, "VOLATILE": 15.0}[state]
        return {"state": state, "score": score, "is_extreme": state == "VOLATILE",
                "reason": f"{basis} → {state}", "atr_pct": atr_pct, "vix": vix}


# ═════════════════════════════════════════════════════════════════════════════
# COMPONENT 4 — SECTOR PARTICIPATION  (consumes Phase 2)
# ═════════════════════════════════════════════════════════════════════════════
class SectorParticipationAnalyzer:
    """How broad is sector leadership? Few sectors carrying everything = fragile."""

    def analyze(self, sector_snapshot: SectorSnapshot) -> dict:
        rows = sector_snapshot.rows
        n = len(rows) or 1
        leaders = [r.sector for r in rows if r.status == "LEADING"]
        improving = [r.sector for r in rows if r.status == "IMPROVING"]
        lagging = [r.sector for r in rows if r.status == "LAGGING"]
        pos_6m = sum(1 for r in rows if (r.r6m or 0) > 0)

        pct_pos = pos_6m / n
        lead_factor = min(1.0, (len(leaders) + len(improving)) / max(1, n * 0.35))
        score = round(100 * (0.65 * pct_pos + 0.35 * lead_factor), 1)
        return {"score": score, "leaders": leaders, "improving": improving,
                "lagging": lagging,
                "reason": f"{pos_6m}/{n} sectors positive (6M), "
                          f"{len(leaders)} leading, {len(improving)} improving"}


# ═════════════════════════════════════════════════════════════════════════════
# COMPONENT 5 — LEADERSHIP QUALITY  (consumes Phase 3/4/5)
# ═════════════════════════════════════════════════════════════════════════════
class LeadershipAnalyzer:
    """Is the market minting fresh leaders, or running out of quality setups?"""

    def analyze(self, rs_snapshot: RelativeStrengthSnapshot,
                composite_snapshot: CompositeSnapshot,
                actionability_snapshot=None) -> dict:
        ml = sum(1 for r in rs_snapshot.rows if r.status == "MARKET_LEADER")
        sl = sum(1 for r in rs_snapshot.rows if r.status == "SECTOR_LEADER")
        a_grades = sum(1 for r in composite_snapshot.rows if r.grade in ("A+", "A"))
        action_now = 0
        if actionability_snapshot is not None:
            action_now = sum(1 for r in actionability_snapshot.rows
                             if r.classification == "ACTION_NOW")

        ml_f = min(1.0, ml / 40)
        a_f = min(1.0, a_grades / 20)
        an_f = min(1.0, action_now / 5)
        score = round(100 * (0.40 * ml_f + 0.35 * a_f + 0.25 * an_f), 1)
        return {"score": score, "market_leaders": ml, "sector_leaders": sl,
                "a_grades": a_grades, "action_now": action_now,
                "reason": f"{ml} market leaders, {a_grades} A/A+ grades, "
                          f"{action_now} ACTION_NOW"}


# ═════════════════════════════════════════════════════════════════════════════
# SNAPSHOT
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class RegimeSnapshot:
    date:                str
    week_id:             str
    regime:              str
    regime_score:        float
    trend_score:         float
    breadth_score:       float
    leadership_score:    float
    participation_score: float
    volatility_score:    float
    trend_state:         str
    volatility_state:    str
    summary:             list[str] = field(default_factory=list)
    detail:              dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def export_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict) -> "RegimeSnapshot":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def save(self, history_dir: Path = _HISTORY_DIR,
             history_csv: Path = _HISTORY_CSV) -> Path:
        history_dir.mkdir(parents=True, exist_ok=True)
        path = history_dir / f"{self.week_id}.json"
        path.write_text(self.export_json())
        try:
            new = pd.DataFrame([{
                "week_id": self.week_id, "date": self.date, "regime": self.regime,
                "regime_score": self.regime_score, "trend": self.trend_score,
                "breadth": self.breadth_score, "leadership": self.leadership_score,
                "participation": self.participation_score,
                "volatility": self.volatility_score,
                "trend_state": self.trend_state, "volatility_state": self.volatility_state,
                "summary": " | ".join(self.summary),
            }])
            if history_csv.exists():
                old = pd.read_csv(history_csv)
                old = old[old["week_id"] != self.week_id]
                new = pd.concat([old, new], ignore_index=True)
            new.to_csv(history_csv, index=False, encoding="utf-8")
        except Exception as exc:
            _log.warning("Could not append regime history CSV: %s", exc)
        return path


# ═════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR
# ═════════════════════════════════════════════════════════════════════════════
class RegimeClassifier:
    """Fuses the five independent analyzers into a scored, labelled regime."""

    def __init__(self):
        self.trend = TrendRegimeAnalyzer()
        self.breadth = BreadthAnalyzer()
        self.volatility = VolatilityAnalyzer()
        self.participation = SectorParticipationAnalyzer()
        self.leadership = LeadershipAnalyzer()

    def analyze(self, *, ohlcv: dict[str, pd.DataFrame],
                index_dfs: dict[str, pd.DataFrame],
                sector_snapshot: SectorSnapshot,
                rs_snapshot: RelativeStrengthSnapshot,
                composite_snapshot: CompositeSnapshot,
                actionability_snapshot=None,
                vix_df: Optional[pd.DataFrame] = None,
                persist: bool = False) -> RegimeSnapshot:
        nifty = index_dfs.get("Nifty50")
        if nifty is None:
            nifty = next(iter(index_dfs.values()), None)
        t = self.trend.analyze(index_dfs)
        b = self.breadth.analyze(ohlcv)
        v = self.volatility.analyze(nifty, vix_df)
        p = self.participation.analyze(sector_snapshot)
        l = self.leadership.analyze(rs_snapshot, composite_snapshot, actionability_snapshot)

        score = round(
            t["score"] * REGIME_WEIGHTS["trend"]
            + b["score"] * REGIME_WEIGHTS["breadth"]
            + l["score"] * REGIME_WEIGHTS["leadership"]
            + p["score"] * REGIME_WEIGHTS["participation"]
            + v["score"] * REGIME_WEIGHTS["volatility"], 1)

        regime = self._classify(score, t, b, p, l, v)
        summary = self._summarize(t, b, p, l, v)

        snap = RegimeSnapshot(
            date=date.today().isoformat(), week_id=week_id_for(),
            regime=regime, regime_score=score,
            trend_score=t["score"], breadth_score=b["score"],
            leadership_score=l["score"], participation_score=p["score"],
            volatility_score=v["score"],
            trend_state=t["state"], volatility_state=v["state"],
            summary=summary,
            detail={"trend": t, "breadth": b, "volatility": v,
                    "participation": p, "leadership": l},
        )
        if persist:
            snap.save()
        return snap

    @staticmethod
    def _classify(score, t, b, p, l, v) -> str:
        # Volatility shock overrides everything.
        if v.get("is_extreme"):
            return "VOLATILE"

        breadth = b["score"]
        part = p["score"]
        lead = l["score"]
        trend_neutral = t["state"] == "NEUTRAL"

        if score >= 75 and breadth >= 65 and part >= 60 and lead >= 55:
            return "STRONG_BULL"
        if score >= 60:
            return "BULL"
        if score >= 50:
            return "RANGE" if (trend_neutral and breadth < 55) else "NEUTRAL"
        if score >= 40:
            return "RANGE" if (trend_neutral and 40 <= breadth <= 60) else "WEAK_BEAR"
        return "STRONG_BEAR"

    @staticmethod
    def _summarize(t, b, p, l, v) -> list[str]:
        m = b["metrics"]
        out = [f"{m['above_ema50']:.0f}% of stocks above EMA50 "
               f"({m['above_ema20']:.0f}% > EMA20, {m['above_ema200']:.0f}% > EMA200)"]
        leaders = p["leaders"][:3]
        if leaders:
            out.append(f"Sector leadership: {', '.join(leaders)}"
                       + (f" + {len(p['leaders'])-3} more" if len(p["leaders"]) > 3 else ""))
        else:
            out.append("No sectors in clear leadership")
        if l["action_now"]:
            out.append(f"{l['action_now']} ACTION_NOW setups, {l['a_grades']} A/A+ grades")
        else:
            out.append(f"No ACTION_NOW setups ({l['market_leaders']} market leaders, "
                       f"{l['a_grades']} A/A+ grades)")
        out.append(f"Volatility {v['state']} ({v['reason'].split('→')[0].strip()})")
        out.append(f"Index trend {t['state']}")
        return out
