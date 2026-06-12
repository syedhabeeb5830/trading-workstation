"""
scanner/adaptive_scoring.py — Regime-Aware Scoring Engine (Phase 7)
===================================================================
The static Phase-4 weights assume every market behaves the same. They don't.
A breakout-heavy A+ that deserves rank #1 in STRONG_BULL deserves rank #30 in
STRONG_BEAR. This layer re-weights the SAME component scores by the active
market regime (Phase 6) — it never recomputes a component, only its *influence*.

    adaptive = rs*w.rs + sector*w.sector + breakout*w.breakout + trend*w.trend
             + liquidity*w.liquidity + atr*w.atr + freshness*w.freshness   (/Σw)

Weights live in config/regime_profiles.yaml (external — Phase 8 can rewrite them
as it learns). NEUTRAL mirrors the static Phase-4 model and is the baseline
against which promotions/demotions are measured.

Consumes Phase 4 CompositeSnapshot + Phase 6 RegimeSnapshot. Modifies nothing
upstream. Classes: RegimeWeightProfile · WeightProfileFactory · AdaptiveScorer ·
AdaptiveSnapshot.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from scanner.sector_engine import week_id_for
from scanner.composite_engine import CompositeSnapshot
from scanner.regime_engine import RegimeSnapshot

_log = logging.getLogger(__name__)

COMPONENTS = ["rs", "sector", "breakout", "trend", "liquidity", "atr", "freshness"]
_LABEL = {"rs": "RS/Leadership", "sector": "Sector", "breakout": "Breakout",
          "trend": "Trend", "liquidity": "Liquidity", "atr": "ATR",
          "freshness": "Freshness"}

_PROFILE_PATH = Path("config/regime_profiles.yaml")
_HISTORY_DIR = Path("Journal/adaptive_history")
_HISTORY_CSV = Path("Journal/adaptive_history.csv")

# Emergency fallback ONLY (the YAML is the source of truth). Mirrors the file so
# the engine still runs if config/regime_profiles.yaml is missing or corrupt.
_DEFAULT_PROFILES: dict[str, dict[str, float]] = {
    "STRONG_BULL": {"rs": 30, "sector": 25, "breakout": 20, "trend": 10, "liquidity": 5, "atr": 5, "freshness": 5},
    "BULL":        {"rs": 28, "sector": 22, "breakout": 20, "trend": 15, "liquidity": 5, "atr": 5, "freshness": 5},
    "NEUTRAL":     {"rs": 25, "sector": 20, "breakout": 20, "trend": 15, "liquidity": 10, "atr": 5, "freshness": 5},
    "RANGE":       {"rs": 20, "sector": 20, "breakout": 10, "trend": 25, "liquidity": 10, "atr": 10, "freshness": 5},
    "WEAK_BEAR":   {"rs": 20, "sector": 20, "breakout": 10, "trend": 30, "liquidity": 10, "atr": 5, "freshness": 5},
    "STRONG_BEAR": {"rs": 20, "sector": 20, "breakout": 5, "trend": 35, "liquidity": 10, "atr": 5, "freshness": 5},
    "VOLATILE":    {"rs": 15, "sector": 15, "breakout": 5, "trend": 30, "liquidity": 20, "atr": 10, "freshness": 5},
}


# ─────────────────────────────────────────────────────────────────────────────
# YAML LOADING (PyYAML if present, else a minimal block parser)
# ─────────────────────────────────────────────────────────────────────────────
def _parse_block_yaml(text: str) -> dict[str, dict[str, float]]:
    """Minimal parser for our simple block-style profiles file (no PyYAML dep)."""
    data: dict[str, dict[str, float]] = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()         # drop comments
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")) and line.rstrip().endswith(":"):
            current = line.strip()[:-1].strip()
            data[current] = {}
        elif line.startswith((" ", "\t")) and ":" in line and current is not None:
            k, v = line.strip().split(":", 1)
            try:
                data[current][k.strip()] = float(v.strip())
            except ValueError:
                pass
    return data


def _load_profiles(path: Path) -> tuple[dict[str, dict[str, float]], str]:
    """Returns (profiles, source). Resilient: file → emergency dict."""
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
            try:
                import yaml  # type: ignore
                data = yaml.safe_load(text)
            except ImportError:
                data = _parse_block_yaml(text)
            if isinstance(data, dict) and data:
                clean = {reg: {k: float(v) for k, v in w.items()}
                         for reg, w in data.items() if isinstance(w, dict)}
                if clean:
                    return clean, f"yaml ({path})"
        except Exception as exc:
            _log.warning("Could not load %s: %s — using built-in defaults.", path, exc)
    _log.warning("regime_profiles.yaml unavailable — using built-in default profiles.")
    return {k: dict(v) for k, v in _DEFAULT_PROFILES.items()}, "builtin_default"


# ─────────────────────────────────────────────────────────────────────────────
# PROFILE + FACTORY
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class RegimeWeightProfile:
    regime:  str
    weights: dict[str, float]

    def total(self) -> float:
        return round(sum(self.weights.values()), 2)

    def adaptive_score(self, components: dict[str, float]) -> float:
        tot = sum(self.weights.values()) or 1.0
        s = sum(float(components.get(k, 0.0)) * self.weights.get(k, 0.0)
                for k in self.weights) / tot
        return round(max(0.0, min(100.0, s)), 2)


class WeightProfileFactory:
    """Loads and serves regime weight profiles from the external YAML."""

    def __init__(self, path: Path = _PROFILE_PATH):
        self._profiles, self.source = _load_profiles(path)

    def for_regime(self, regime: str) -> RegimeWeightProfile:
        w = self._profiles.get(regime) or self._profiles.get("NEUTRAL") \
            or dict(_DEFAULT_PROFILES["NEUTRAL"])
        return RegimeWeightProfile(regime, dict(w))

    def all_profiles(self) -> dict[str, RegimeWeightProfile]:
        return {r: RegimeWeightProfile(r, dict(w)) for r, w in self._profiles.items()}

    def regimes(self) -> list[str]:
        return list(self._profiles)


# ─────────────────────────────────────────────────────────────────────────────
# OUTPUT TYPES
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class AdaptiveRow:
    ticker:          str
    symbol:          str
    sector:          str
    composite_score: float
    composite_rank:  int
    adaptive_score:  float
    adaptive_rank:   int
    score_change:    float
    rank_change:     int          # composite_rank − adaptive_rank (+ = promoted)
    regime:          str
    weight_profile:  str
    reason:          list[str] = field(default_factory=list)


@dataclass
class AdaptiveSnapshot:
    rows:           list[AdaptiveRow]
    regime:         str
    weight_profile: str
    weights:        dict[str, float]
    generated_at:   str
    week_id:        str

    def top_n(self, n: int = 20) -> list[AdaptiveRow]:
        return self.rows[:n]

    def top_promotions(self, n: int = 5) -> list[AdaptiveRow]:
        return sorted([r for r in self.rows if r.score_change > 0],
                      key=lambda r: -r.score_change)[:n]

    def top_demotions(self, n: int = 5) -> list[AdaptiveRow]:
        return sorted([r for r in self.rows if r.score_change < 0],
                      key=lambda r: r.score_change)[:n]

    def get(self, ticker: str) -> Optional[AdaptiveRow]:
        return next((r for r in self.rows if r.ticker == ticker), None)

    def to_dict(self) -> dict:
        return {"regime": self.regime, "weight_profile": self.weight_profile,
                "weights": self.weights, "generated_at": self.generated_at,
                "week_id": self.week_id, "rows": [asdict(r) for r in self.rows]}

    def export_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def save(self, history_dir: Path = _HISTORY_DIR,
             history_csv: Path = _HISTORY_CSV) -> Path:
        history_dir.mkdir(parents=True, exist_ok=True)
        path = history_dir / f"{self.week_id}.json"
        path.write_text(self.export_json())
        try:
            new = pd.DataFrame([{
                "week_id": self.week_id, "date": self.generated_at[:10],
                "regime": self.regime, "weight_profile": self.weight_profile,
                "ticker": r.ticker, "composite_score": r.composite_score,
                "adaptive_score": r.adaptive_score, "delta": r.score_change,
                "composite_rank": r.composite_rank, "adaptive_rank": r.adaptive_rank,
            } for r in self.rows])
            if history_csv.exists():
                old = pd.read_csv(history_csv)
                old = old[old["week_id"] != self.week_id]
                new = pd.concat([old, new], ignore_index=True)
            new.to_csv(history_csv, index=False, encoding="utf-8")
        except Exception as exc:
            _log.warning("Could not append adaptive history CSV: %s", exc)
        return path


# ─────────────────────────────────────────────────────────────────────────────
# SCORER
# ─────────────────────────────────────────────────────────────────────────────
class AdaptiveScorer:
    """Re-weights Phase-4 components by the active regime profile."""

    def __init__(self, factory: Optional[WeightProfileFactory] = None,
                 baseline_regime: str = "NEUTRAL"):
        self.factory = factory or WeightProfileFactory()
        self.baseline = self.factory.for_regime(baseline_regime).weights

    def score(self, composite_snapshot: CompositeSnapshot,
              regime_snapshot: RegimeSnapshot, persist: bool = False) -> AdaptiveSnapshot:
        profile = self.factory.for_regime(regime_snapshot.regime)

        rows: list[AdaptiveRow] = []
        for cr in composite_snapshot.rows:
            comp = cr.components
            adaptive = profile.adaptive_score(comp)
            rows.append(AdaptiveRow(
                cr.ticker, cr.symbol, cr.sector, cr.score, cr.rank,
                adaptive, 0, round(adaptive - cr.score, 2), 0,
                regime_snapshot.regime, regime_snapshot.regime,
                self._reason(comp, profile.weights),
            ))

        rows.sort(key=lambda r: (-r.adaptive_score, r.ticker))
        for i, r in enumerate(rows, 1):
            r.adaptive_rank = i
            r.rank_change = r.composite_rank - r.adaptive_rank   # + = promoted

        snap = AdaptiveSnapshot(
            rows, regime_snapshot.regime, regime_snapshot.regime,
            dict(profile.weights), datetime.now().isoformat(timespec="seconds"),
            week_id_for())
        if persist:
            snap.save()
        return snap

    def _reason(self, comp: dict[str, float], w: dict[str, float]) -> list[str]:
        """Per-stock explanation: which re-weightings moved this name, by how much."""
        contrib = {k: float(comp.get(k, 0.0)) * (w.get(k, 0.0) - self.baseline.get(k, 0.0)) / 100.0
                   for k in w}
        pos = sorted((k for k in contrib if contrib[k] > 0.05), key=lambda k: -contrib[k])[:2]
        neg = sorted((k for k in contrib if contrib[k] < -0.05), key=lambda k: contrib[k])[:2]
        out = [f"{_LABEL[k]} weight up → +{contrib[k]:.1f}" for k in pos]
        out += [f"{_LABEL[k]} weight down → {contrib[k]:.1f}" for k in neg]
        return out or ["Profile matches baseline (no change)"]
