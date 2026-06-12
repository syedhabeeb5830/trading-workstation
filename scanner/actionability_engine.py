"""
scanner/actionability_engine.py — Actionability Engine (Phase 5)
================================================================
Phases 1-4 answer "how strong is this stock?". Phase 5 answers the harder,
more important question: "should I trade it TODAY?"

A name can be Composite 95 and still be a poor trade — too extended above its
breakout, miles from support, or carrying a lousy risk/reward. This layer sits
entirely ON TOP of Phase 4 (it never recomputes RS, sector, or composite) and
adds an independent judgement of entry location and trade geometry.

    actionability = composite*0.70 + entry_quality*0.15 + risk_reward*0.15

Then every stock is labelled exactly one of:
    ACTION_NOW   strong AND well-located AND RR ≥ 2   → trade today
    WATCHLIST    strong but entry not ideal           → monitor
    EXTENDED     strong but stretched above breakout  → wait for pullback
    AVOID        weak setup or poor RR                → ignore

Scorers are independent (none references another). ActionabilityRanker
orchestrates; ActionabilitySnapshot stores, persists, and exports the board.

Every row persists entry/stop/target/RR/classification/actionability — the
foundation for a future Phase-6 Outcome Tracking & Expectancy engine.
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
from scanner.sector_engine import week_id_for
from scanner.composite_engine import CompositeSnapshot
from scanner.entry_context import EntryContext, EntryContextClassifier

_log = logging.getLogger(__name__)

# ── Actionability blend (composite stays dominant) ───────────────────────────
ACT_WEIGHTS = {"composite": 0.70, "entry_quality": 0.15, "risk_reward": 0.15}

# ── Classification thresholds ────────────────────────────────────────────────
ACTION_NOW_COMPOSITE = 80
ACTION_NOW_ACT       = 80
ACTION_NOW_RR        = 2.0
STRONG_COMPOSITE     = 65
EXTENDED_DIST_THRESHOLD = 6.0     # legacy: % above breakout
EXTENDED_EXT_THRESHOLD  = 8.0     # % stretched (above breakout OR above EMA20) → EXTENDED
MIN_RR_FLOOR         = 1.0        # below this RR → AVOID
# Contexts that represent a tradable entry right now (vs base-building / no-setup).
TRADABLE_CONTEXTS = {"PULLBACK_SETUP", "BREAKOUT_SETUP", "TREND_CONTINUATION"}

_HISTORY_DIR = Path("Journal/actionability_history")
_HISTORY_CSV = Path("Journal/actionability_history.csv")


def _fmt(p: float) -> str:
    """₹ price formatter — no decimals above 100, two below."""
    if p is None:
        return "—"
    return f"₹{p:,.0f}" if p >= 100 else f"₹{p:,.2f}"


# ═════════════════════════════════════════════════════════════════════════════
# INDEPENDENT SCORERS
# ═════════════════════════════════════════════════════════════════════════════
class EntryQualityScorer:
    """Is the current price a GOOD place to enter? Judged IN CONTEXT of the setup
    type — a pullback to support in an uptrend is a great entry even though it
    sits below the breakout (the old logic wrongly punished exactly this)."""

    def score(self, df: pd.DataFrame, context: "EntryContext") -> dict:
        ctx = context.context
        ext = context.ema20_ext              # % vs EMA20 (+ above)
        sd = ((context.price - context.ll20) / context.price * 100
              if context.price else 99.0)    # distance above support

        # Context sets the base score; "good entry" means different things per setup.
        if ctx == "PULLBACK_SETUP":
            base = (100 if -2 <= ext <= 4 else 85 if -4 <= ext < -2
                    else 80 if 4 < ext <= 6 else 65)
        elif ctx == "BREAKOUT_SETUP":
            base = (100 if context.extension_pct <= 2.5 else 85 if context.extension_pct <= 4
                    else 60 if context.extension_pct <= 7 else 35)
        elif ctx == "TREND_CONTINUATION":
            base = (70 if context.extension_pct <= 8 else 50 if context.extension_pct <= 12 else 30)
        elif ctx == "BASE_BUILDING":
            base = 50
        else:                                # NO_SETUP
            base = 20

        # Tighter stop (closer support) refines the entry quality a little.
        tight = 10 if sd <= 5 else 0 if sd <= 9 else -10 if sd <= 14 else -20
        score = round(max(0.0, min(100.0, base + tight)), 1)
        breakout_dist = round(-context.dist_below_high, 2)   # + = above breakout
        return {"score": score, "breakout_distance_pct": breakout_dist,
                "extension_pct": context.extension_pct,
                "reason": f"{ctx} · {ext:+.1f}% vs EMA20 · {sd:.1f}% above support"}


class RiskRewardScorer:
    """Trade geometry, IN CONTEXT: a pullback stops just under the rising EMA20 and
    targets the high it pulled back from (tight risk, clear reward); a breakout
    stops under the base and targets a measured move; momentum uses a wider ATR
    stop. The old one-size logic gave dip-buys lousy RR and mis-rated everything."""

    def score(self, df: pd.DataFrame, context: "EntryContext") -> dict:
        price = float(df["Close"].iloc[-1])
        atr = float(compute_atr_series(df).iloc[-1])
        low, high = df["Low"], df["High"]
        swing_low = float(low.tail(10).min())
        ctx = context.context
        h20, ll20, ema20 = context.h20, context.ll20, context.ema20

        if ctx == "PULLBACK_SETUP":
            stop = min(ema20 * 0.985, swing_low * 0.99)          # just below the dip
            target = max(h20, price * 1.03)                       # reclaim the prior high
        elif ctx == "BREAKOUT_SETUP":
            stop = ll20 * 0.99                                    # below the base
            target = h20 + (h20 - ll20)                           # measured move
        elif ctx == "TREND_CONTINUATION":
            stop = price - 2.0 * atr                              # wider, momentum
            measured = h20 + (h20 - float(low.tail(40).min()))
            target = max(measured, price * 1.05)
        elif ctx == "BASE_BUILDING":
            stop = ll20 * 0.99                                    # anticipatory breakout
            target = h20 + (h20 - ll20)
        else:                                                    # NO_SETUP — conservative
            stop = min(price - 1.5 * atr, swing_low * 0.99)
            target = max(float(high.tail(250).max()), price * 1.02)

        # Floor: never tighter than ~1 ATR or 1.5% — kills noise stops.
        min_risk = max(1.0 * atr, price * 0.015)
        if price - stop < min_risk:
            stop = price - min_risk
        risk = max(price - stop, 1e-9)
        reward = max(target - price, 0.0)
        rr = round(reward / risk, 2)

        return {"score": float(self._rr_score(rr)), "rr_ratio": rr,
                "entry": round(price, 2), "stop": round(stop, 2), "target": round(target, 2),
                "stop_zone": _fmt(stop), "target_zone": _fmt(target),
                "reason": f"{ctx} RR {rr:.1f} (risk {_fmt(price)}→{_fmt(stop)}, →{_fmt(target)})"}

    @staticmethod
    def _rr_score(rr: float) -> float:
        if rr > 3:   return 100.0
        if rr >= 2:  return 70.0
        if rr >= 1:  return 30.0
        return 0.0


class ActionabilityScorer:
    """Pure blend of three numbers — knows nothing about how they were produced."""

    def score(self, composite: float, entry_quality: float, risk_reward: float) -> dict:
        s = (composite * ACT_WEIGHTS["composite"]
             + entry_quality * ACT_WEIGHTS["entry_quality"]
             + risk_reward * ACT_WEIGHTS["risk_reward"])
        return {"score": round(max(0.0, min(100.0, s)), 2)}


# ═════════════════════════════════════════════════════════════════════════════
# OUTPUT TYPES
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class ActionabilityRow:
    ticker:               str
    symbol:               str
    sector:               str
    rank:                 int
    composite_score:      float
    actionability_score:  float
    grade:                str
    classification:       str
    rr_ratio:             float
    entry:                float
    stop:                 float
    target:               float
    entry_zone:           str
    stop_zone:            str
    target_zone:          str
    breakout_distance_pct: float
    entry_context:        str
    extension_pct:        float
    components:           dict[str, float]
    drivers:              list[str]
    warnings:             list[str]
    rs_status:            str
    days_since_breakout:  Optional[int]


@dataclass
class ActionabilitySnapshot:
    rows:         list[ActionabilityRow]
    generated_at: str
    week_id:      str

    # ── Consumption APIs ─────────────────────────────────────────────────────
    def top_n(self, n: int = 20) -> list[ActionabilityRow]:
        return self.rows[:n]

    def action_now(self, n: int = 5) -> list[ActionabilityRow]:
        return [r for r in self.rows if r.classification == "ACTION_NOW"][:n]

    def classification_distribution(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows:
            out[r.classification] = out.get(r.classification, 0) + 1
        return {c: out.get(c, 0) for c in
                ("ACTION_NOW", "WATCHLIST", "EXTENDED", "AVOID")}

    def get(self, ticker: str) -> Optional[ActionabilityRow]:
        return next((r for r in self.rows if r.ticker == ticker), None)

    # ── Serialisation ────────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {"generated_at": self.generated_at, "week_id": self.week_id,
                "rows": [asdict(r) for r in self.rows]}

    def export_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, d: dict) -> "ActionabilitySnapshot":
        rows = [ActionabilityRow(**r) for r in d.get("rows", [])]
        return cls(rows, d.get("generated_at", ""), d.get("week_id", ""))

    def to_csv(self) -> str:
        cols = ["rank", "ticker", "sector", "composite_score", "actionability_score",
                "grade", "classification", "rr_ratio", "entry", "stop", "target",
                "entry_zone", "stop_zone", "target_zone", "breakout_distance_pct",
                "entry_context", "extension_pct",
                "rs_status", "days_since_breakout", "drivers", "warnings"]
        recs = []
        for r in self.rows:
            d = asdict(r)
            d["drivers"] = " | ".join(r.drivers)
            d["warnings"] = " | ".join(r.warnings)
            recs.append({c: d.get(c) for c in cols})
        return pd.DataFrame(recs, columns=cols).to_csv(index=False)

    def to_markdown(self, top: int = 20) -> str:
        lines = [f"# Swing Screen — {self.generated_at[:10]}", ""]
        cd = self.classification_distribution()
        lines.append("**Classification:** " +
                     "  ".join(f"{k}={v}" for k, v in cd.items()))
        lines += ["", "## Top 20 Candidates", "",
                  "| # | Ticker | Comp | Act | Grade | Class | RR | Entry | Stop | Target | Drivers |",
                  "|--:|--------|-----:|----:|:-----:|:------|---:|------:|-----:|-------:|:--------|"]
        for r in self.top_n(top):
            lines.append(
                f"| {r.rank} | {r.symbol} | {r.composite_score:.1f} | "
                f"{r.actionability_score:.1f} | {r.grade} | {r.classification} | "
                f"{r.rr_ratio:.1f} | {r.entry_zone} | {r.stop_zone} | {r.target_zone} | "
                f"{', '.join(r.drivers[:3])} |")
        lines += ["", "## Top 5 Actionable Trades", ""]
        an = self.action_now(5)
        if not an:
            lines.append("_No ACTION_NOW setups today._")
        for i, r in enumerate(an, 1):
            lines.append(f"{i}. **{r.symbol}** — Composite {r.composite_score:.1f}, "
                         f"Actionability {r.actionability_score:.1f}, RR {r.rr_ratio:.1f}, "
                         f"{r.classification}  \n   Entry {r.entry_zone} · Stop {r.stop_zone} "
                         f"· Target {r.target_zone}")
        return "\n".join(lines) + "\n"

    def save(self, history_dir: Path = _HISTORY_DIR,
             history_csv: Path = _HISTORY_CSV) -> Path:
        history_dir.mkdir(parents=True, exist_ok=True)
        path = history_dir / f"{self.week_id}.json"
        path.write_text(self.export_json())
        try:
            new = pd.DataFrame([{
                "week_id": self.week_id, "date": self.generated_at[:10],
                "rank": r.rank, "ticker": r.ticker, "sector": r.sector,
                "composite_score": r.composite_score,
                "actionability_score": r.actionability_score,
                "grade": r.grade, "classification": r.classification,
                "rr_ratio": r.rr_ratio, "entry": r.entry, "stop": r.stop,
                "target": r.target, "breakout_distance_pct": r.breakout_distance_pct,
                "entry_context": r.entry_context, "rs_status": r.rs_status,
                "drivers": " | ".join(r.drivers), "warnings": " | ".join(r.warnings),
            } for r in self.rows])
            if history_csv.exists():
                old = pd.read_csv(history_csv)
                old = old[old["week_id"] != self.week_id]
                new = pd.concat([old, new], ignore_index=True)
            new.to_csv(history_csv, index=False, encoding="utf-8")
        except Exception as exc:
            _log.warning("Could not append actionability history CSV: %s", exc)
        return path


# ═════════════════════════════════════════════════════════════════════════════
# ORCHESTRATOR
# ═════════════════════════════════════════════════════════════════════════════
class ActionabilityRanker:
    """
    Sits on top of Phase 4: for every composite survivor, score entry quality
    and risk/reward, blend into an actionability score, classify, and rank.

    Parameters
    ----------
    composite_snapshot : Phase-4 CompositeSnapshot (source of strength + drivers)
    ohlcv              : {ticker: DataFrame} (1y) — for entry/stop/target geometry
    """

    def __init__(self, composite_snapshot: CompositeSnapshot,
                 ohlcv: dict[str, pd.DataFrame]):
        self.composite = composite_snapshot
        self.ohlcv = ohlcv
        self.context = EntryContextClassifier()
        self.entry_q = EntryQualityScorer()
        self.rr = RiskRewardScorer()
        self.act = ActionabilityScorer()

    def rank(self, persist: bool = False) -> ActionabilitySnapshot:
        rows: list[ActionabilityRow] = []
        for cr in self.composite.rows:
            df = self.ohlcv.get(cr.ticker)
            if df is None or df.empty or len(df) < 50:
                continue

            ctx = self.context.classify(df)
            eq = self.entry_q.score(df, ctx)
            rr = self.rr.score(df, ctx)
            act = self.act.score(cr.score, eq["score"], rr["score"])

            classification = self._classify(cr.score, act["score"], rr["rr_ratio"], ctx)
            drivers, warnings = self._explain(cr, rr, eq, ctx, classification)

            entry_low = min(rr["entry"], cr.breakout_level)
            entry_high = round(rr["entry"] * 1.005, 2)
            entry_zone = f"{_fmt(entry_low)} - {_fmt(entry_high)}"

            rows.append(ActionabilityRow(
                cr.ticker, cr.symbol, cr.sector, 0,
                cr.score, act["score"], cr.grade, classification, rr["rr_ratio"],
                rr["entry"], rr["stop"], rr["target"],
                entry_zone, rr["stop_zone"], rr["target_zone"],
                eq["breakout_distance_pct"], ctx.context, ctx.extension_pct,
                {"composite": cr.score, "entry_quality": eq["score"],
                 "risk_reward": rr["score"]},
                drivers, warnings, cr.rs_status, cr.days_since_breakout,
            ))

        # Decision ordering: actionability desc, composite desc, ticker asc.
        rows.sort(key=lambda r: (-r.actionability_score, -r.composite_score, r.ticker))
        for i, r in enumerate(rows, 1):
            r.rank = i

        snap = ActionabilitySnapshot(rows, datetime.now().isoformat(timespec="seconds"),
                                     week_id_for())
        if persist:
            snap.save()
        return snap

    @staticmethod
    def _classify(composite: float, act: float, rr: float, ctx: "EntryContext") -> str:
        # Weak strength, no constructive structure, or broken RR → never trade.
        if composite < STRONG_COMPOSITE or ctx.context == "NO_SETUP" or rr < MIN_RR_FLOOR:
            return "AVOID"
        # Stretched (above breakout OR far above EMA20) → wait for a pullback, even if a
        # measured-move target makes RR look good. Checked BEFORE ACTION_NOW so we never
        # tag a chase as "buy now".
        if ctx.extension_pct > EXTENDED_EXT_THRESHOLD:
            return "EXTENDED"
        # Strong + well-located + a real tradable setup + not stretched + RR ≥ 2.
        if (composite >= ACTION_NOW_COMPOSITE and act >= ACTION_NOW_ACT
                and rr >= ACTION_NOW_RR and ctx.context in TRADABLE_CONTEXTS):
            return "ACTION_NOW"
        return "WATCHLIST"

    @staticmethod
    def _explain(cr, rr, eq, ctx, classification) -> tuple[list[str], list[str]]:
        drivers = list(cr.drivers)          # inherit Phase-4 strength drivers
        warnings = list(cr.warnings)

        drivers.append(ctx.context.replace("_", " ").title())   # name the setup
        if rr["rr_ratio"] >= ACTION_NOW_RR:
            drivers.append(f"RR {rr['rr_ratio']:.1f}:1")
        if classification == "EXTENDED" or ctx.extension_pct > EXTENDED_EXT_THRESHOLD:
            warnings.append(f"Extended {ctx.extension_pct:.1f}% (vs breakout/EMA20)")
        if rr["rr_ratio"] < MIN_RR_FLOOR:
            warnings.append(f"Poor RR ({rr['rr_ratio']:.1f})")
        risk_pct = (rr["entry"] - rr["stop"]) / rr["entry"] * 100 if rr["entry"] else 0
        if risk_pct > 8:
            warnings.append(f"Wide stop ({risk_pct:.0f}%)")

        # De-dupe, preserve order, guarantee non-empty drivers.
        drivers = list(dict.fromkeys(drivers)) or [f"Composite {cr.score:.0f}"]
        warnings = list(dict.fromkeys(warnings))
        return drivers, warnings
