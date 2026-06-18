"""
portfolio/portfolio_state.py — Real Portfolio State Layer
==========================================================
Bridges the hypothetical allocation engine (PortfolioConstructor → %)
with broker reality: actual positions, quantities, cash balance, and P&L.

Three sources tried in priority order:
  A) ZERODHA_LIVE   — KiteClient.holdings_df() + margins_equity()
  B) MANUAL_CSV     — state/holdings.csv  (ticker, qty, avg_price)
  C) SIMULATED      — last PortfolioSnapshot JSON (Journal/portfolio_history/)

Persists to: state/portfolio_state.json (updated by --sync-portfolio).
Everything else is read-only.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

_log = logging.getLogger(__name__)

_STATE_DIR      = Path("state")
_STATE_FILE     = _STATE_DIR / "portfolio_state.json"
_MANUAL_CSV     = Path("state/holdings.csv")
_UNIVERSE_CACHE = Path("cache/universe/nifty500.csv")
_PORTFOLIO_HIST = Path("Journal/portfolio_history")


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _load_sector_map() -> dict[str, str]:
    """Load {ticker.NS → sector} from the cached Nifty-500 universe."""
    if not _UNIVERSE_CACHE.exists():
        return {}
    try:
        df = pd.read_csv(_UNIVERSE_CACHE)
        return dict(zip(df["ticker"].str.strip().str.upper(),
                        df["sector"].fillna("DIVERSIFIED")))
    except Exception:
        return {}


def _load_portfolio_stops() -> dict[str, float]:
    """Pull {symbol → stop_price} from the most recent portfolio snapshot JSON."""
    if not _PORTFOLIO_HIST.exists():
        return {}
    files = sorted(_PORTFOLIO_HIST.glob("*.json"))
    if not files:
        return {}
    try:
        data = json.loads(files[-1].read_text(encoding="utf-8"))
        out: dict[str, float] = {}
        for p in data.get("positions", []):
            sym = str(p.get("ticker", "")).upper().replace(".NS", "")
            stop = p.get("stop")
            if sym and stop:
                out[sym] = float(stop)
        return out
    except Exception:
        return {}


def _find_entry_dates() -> dict[str, str]:
    """Scan portfolio_history/*.json to find the first date each symbol appeared.
    Returns {symbol_without_NS: ISO-date-string}.
    """
    if not _PORTFOLIO_HIST.exists():
        return {}
    out: dict[str, str] = {}
    for path in sorted(_PORTFOLIO_HIST.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            when = (data.get("generated_at") or "")[:10]
            if not when:
                continue
            for p in data.get("positions", []):
                sym = str(p.get("ticker", "")).upper().replace(".NS", "")
                if sym and sym not in out:
                    out[sym] = when
        except Exception:
            continue
    return out


def _fetch_prices_yf(tickers: list[str]) -> dict[str, float]:
    """Last close price per ticker via yfinance. Falls back gracefully."""
    out: dict[str, float] = {}
    if not tickers:
        return out
    try:
        import yfinance as yf
        raw = yf.download(tickers, period="5d", auto_adjust=True,
                          progress=False, threads=True)
        close = raw.get("Close", raw.get("Adj Close", raw))
        if hasattr(close, "columns"):
            for t in tickers:
                col = next((c for c in close.columns if c == t), None)
                if col is not None:
                    s = close[col].dropna()
                    if not s.empty:
                        out[t] = float(s.iloc[-1])
        else:
            s = close.dropna()
            if not s.empty and tickers:
                out[tickers[0]] = float(s.iloc[-1])
    except Exception as exc:
        _log.warning("yfinance price fetch failed: %s", exc)
    return out


def _parse_kite_orders(raw: list[dict]) -> list["OpenOrder"]:
    """Convert raw Kite order dicts to OpenOrder; keep only pending CNC/NSE orders."""
    out = []
    for o in raw or []:
        if str(o.get("product", "")).upper() not in ("CNC", "DELIVERY"):
            continue
        if str(o.get("exchange", "")).upper() not in ("NSE", "BSE"):
            continue
        status = str(o.get("status", "")).upper()
        if status in ("CANCELLED", "REJECTED", "COMPLETE"):
            continue
        sym = str(o.get("tradingsymbol", "")).upper()
        ticker = f"{sym}.NS"
        side = "BUY" if str(o.get("transaction_type", "")).upper() == "BUY" else "SELL"
        out.append(OpenOrder(
            order_id=str(o.get("order_id", "")),
            ticker=ticker, symbol=sym, side=side,
            quantity=int(o.get("quantity", 0) or 0),
            price=float(o.get("price", 0) or 0),
            status=status or "OPEN"
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# DATA TYPES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LiveHolding:
    ticker:             str            # with .NS suffix
    symbol:             str            # without .NS (display / matching key)
    sector:             str
    quantity:           int
    avg_price:          float
    current_price:      float
    current_value:      float          # quantity × current_price
    cost_basis:         float          # quantity × avg_price
    unrealized_pnl:     float          # current_value − cost_basis
    unrealized_pnl_pct: float
    allocation_pct:     float          # current_value / total_capital × 100
    stop_price:         Optional[float]
    entry_date:         Optional[str] = None   # ISO date of first appearance in portfolio

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LiveHolding":
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})   # type: ignore[attr-defined]


@dataclass
class OpenOrder:
    order_id: str
    ticker:   str
    symbol:   str
    side:     str       # BUY | SELL
    quantity: int
    price:    float
    status:   str       # OPEN | PENDING | TRIGGER_PENDING

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PortfolioState:
    as_of:          str            # ISO timestamp
    source:         str            # ZERODHA_LIVE | MANUAL_CSV | SIMULATED | CACHED
    total_capital:  float
    holdings_value: float          # Σ quantity × current_price
    available_cash: float          # free cash in the equity segment
    net_equity:     float          # holdings_value + available_cash
    deployed_pct:   float          # holdings_value / total_capital × 100
    cash_pct:       float          # available_cash / total_capital × 100
    holdings:       list[LiveHolding]
    open_orders:    list[OpenOrder]
    unrealized_pnl: float          # Σ unrealized_pnl across holdings
    realized_pnl:   float = 0.0   # session-level realized P&L (from tradebook, best-effort)

    # ── serialisation ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "as_of":          self.as_of,
            "source":         self.source,
            "total_capital":  self.total_capital,
            "holdings_value": self.holdings_value,
            "available_cash": self.available_cash,
            "net_equity":     self.net_equity,
            "deployed_pct":   self.deployed_pct,
            "cash_pct":       self.cash_pct,
            "holdings":       [h.to_dict() for h in self.holdings],
            "open_orders":    [o.to_dict() for o in self.open_orders],
            "unrealized_pnl": self.unrealized_pnl,
            "realized_pnl":   self.realized_pnl,
        }

    def save(self, path: Path = _STATE_FILE) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path = _STATE_FILE) -> Optional["PortfolioState"]:
        if not path.exists():
            return None
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            holdings = [LiveHolding.from_dict(h) for h in d.get("holdings", [])]
            orders   = [OpenOrder(**o) for o in d.get("open_orders", [])]
            return cls(
                as_of=d["as_of"], source=d["source"],
                total_capital=float(d["total_capital"]),
                holdings_value=float(d["holdings_value"]),
                available_cash=float(d["available_cash"]),
                net_equity=float(d["net_equity"]),
                deployed_pct=float(d["deployed_pct"]),
                cash_pct=float(d["cash_pct"]),
                holdings=holdings, open_orders=orders,
                unrealized_pnl=float(d.get("unrealized_pnl", 0)),
                realized_pnl=float(d.get("realized_pnl", 0.0)),
            )
        except Exception as exc:
            _log.warning("Could not load state file %s: %s", path, exc)
            return None

    # ── lookup helpers ────────────────────────────────────────────────────────

    def holdings_by_symbol(self) -> dict[str, LiveHolding]:
        """Key is symbol WITHOUT .NS (matches PortfolioCandidate.symbol)."""
        return {h.symbol: h for h in self.holdings}


# ─────────────────────────────────────────────────────────────────────────────
# STATE LOADER
# ─────────────────────────────────────────────────────────────────────────────

class StateLoader:
    """Load account state from three sources."""

    # ── A) Zerodha live ─────────────────────────────────────────────────────
    @staticmethod
    def from_kite(capital: float) -> Optional[PortfolioState]:
        """
        Read live holdings + available cash from Zerodha KiteConnect.
        Returns None if no valid session exists (never crashes).
        """
        try:
            from integrations.zerodha import get_client_or_none
            kc = get_client_or_none()
            if kc is None:
                return None
        except Exception:
            return None

        sector_map  = _load_sector_map()
        stops       = _load_portfolio_stops()
        entry_dates = _find_entry_dates()

        # Enrich stops from live Kite GTTs (overrides snapshot-derived stops)
        try:
            for gtt in (kc.list_gtts() or []):
                if str(gtt.get("status", "")).lower() not in ("active", "triggered"):
                    continue
                cond = gtt.get("condition", {}) or {}
                sym = str(cond.get("tradingsymbol", "")).upper()
                triggers = cond.get("trigger_values") or []
                if sym and triggers:
                    stops[sym] = float(triggers[0])   # first trigger = stop price
        except Exception:
            pass

        try:
            df = kc.holdings_df()
        except Exception as exc:
            _log.warning("Kite holdings_df() failed: %s", exc)
            return None

        try:
            margins = kc.margins_equity()
            # Deployable buying power = Kite "net" (live_balance + collateral − debits).
            # NOTE: do NOT use available.cash — that is the *settled* opening balance and
            # reads ₹0 for a same-day fund transfer (which lands as live_balance / payin).
            # Prefer net → live_balance → available_cash (the `or` chain skips 0/falsy).
            available_cash = float(
                margins.get("net")
                or margins.get("live_balance")
                or margins.get("available_cash")
                or 0.0)
        except Exception:
            available_cash = 0.0

        try:
            raw_orders = kc.orders()
        except Exception:
            raw_orders = []

        # G3: session-level realized P&L from tradebook (best-effort)
        realized_pnl = 0.0
        try:
            trades = kc._kite.tradebook() or []
            realized_pnl = round(sum(float(t.get("pnl", 0) or 0) for t in trades), 2)
        except Exception:
            pass

        holdings: list[LiveHolding] = []
        holdings_value = 0.0

        if df is not None and not df.empty:
            for _, row in df.iterrows():
                ticker = str(row.get("ticker", "")).strip().upper()
                if not ticker:
                    continue
                symbol = ticker[:-3] if ticker.endswith(".NS") else ticker
                qty    = int(row.get("quantity", 0) or 0)
                if qty <= 0:
                    continue
                avg_p  = float(row.get("avg_price",  0) or 0)
                last_p = float(row.get("last_price", avg_p) or avg_p)
                pnl    = float(row.get("pnl", 0) or 0)

                cur_val   = qty * last_p
                cost      = qty * avg_p
                upnl_pct  = ((last_p - avg_p) / avg_p * 100) if avg_p else 0.0

                holdings.append(LiveHolding(
                    ticker=ticker, symbol=symbol,
                    sector=sector_map.get(ticker, "DIVERSIFIED"),
                    quantity=qty,
                    avg_price=round(avg_p,  2),
                    current_price=round(last_p, 2),
                    current_value=round(cur_val, 2),
                    cost_basis=round(cost, 2),
                    unrealized_pnl=round(pnl, 2),
                    unrealized_pnl_pct=round(upnl_pct, 2),
                    allocation_pct=0.0,
                    stop_price=stops.get(symbol),
                    entry_date=entry_dates.get(symbol),
                ))
                holdings_value += cur_val

        # G2: use actual broker NAV if it substantially exceeds the configured capital
        actual_nav = holdings_value + available_cash
        if actual_nav > capital * 1.05:
            _log.warning("Kite NAV ₹%.0f > config capital ₹%.0f — using Kite NAV.",
                         actual_nav, capital)
            capital = actual_nav

        for h in holdings:
            h.allocation_pct = round(h.current_value / capital * 100, 2) if capital else 0.0

        net = holdings_value + available_cash
        return PortfolioState(
            as_of=datetime.now().isoformat(timespec="seconds"),
            source="ZERODHA_LIVE",
            total_capital=capital,
            holdings_value=round(holdings_value, 2),
            available_cash=round(available_cash, 2),
            net_equity=round(net, 2),
            deployed_pct=round(holdings_value / capital * 100, 2) if capital else 0.0,
            cash_pct=round(available_cash / capital * 100, 2) if capital else 0.0,
            holdings=holdings,
            open_orders=_parse_kite_orders(raw_orders),
            unrealized_pnl=round(sum(h.unrealized_pnl for h in holdings), 2),
            realized_pnl=realized_pnl,
        )

    # ── B) Manual CSV ────────────────────────────────────────────────────────
    @staticmethod
    def from_csv(capital: float, csv_path: Path = _MANUAL_CSV) -> Optional[PortfolioState]:
        """
        Read from state/holdings.csv.
        Required columns: ticker (or symbol), qty, avg_price.
        Fetches current prices via yfinance.
        Returns None if the file is absent.
        """
        if not csv_path.exists():
            return None
        try:
            df = pd.read_csv(csv_path)
        except Exception as exc:
            _log.warning("Manual CSV read failed: %s", exc)
            return None

        df.columns = df.columns.str.strip().str.lower()
        if "ticker" not in df.columns and "symbol" in df.columns:
            df.rename(columns={"symbol": "ticker"}, inplace=True)
        if "ticker" not in df.columns:
            _log.warning("state/holdings.csv must have a 'ticker' or 'symbol' column")
            return None
        if "qty" not in df.columns or "avg_price" not in df.columns:
            _log.warning("state/holdings.csv must have 'qty' and 'avg_price' columns")
            return None

        df["ticker"] = (df["ticker"].str.strip().str.upper()
                        .apply(lambda t: t if t.endswith((".NS", ".BO")) else f"{t}.NS"))

        tickers     = df["ticker"].tolist()
        prices      = _fetch_prices_yf(tickers)
        sector_map  = _load_sector_map()
        stops       = _load_portfolio_stops()
        entry_dates = _find_entry_dates()

        # Also accept optional entry_date column from the CSV itself
        has_date_col = "entry_date" in df.columns

        holdings: list[LiveHolding] = []
        holdings_value = 0.0

        for _, row in df.iterrows():
            ticker = str(row["ticker"])
            symbol = ticker[:-3] if ticker.endswith(".NS") else ticker
            qty    = int(row.get("qty", 0) or 0)
            if qty <= 0:
                continue
            avg_p  = float(row.get("avg_price", 0) or 0)
            last_p = prices.get(ticker, avg_p)
            cur_val = qty * last_p
            cost    = qty * avg_p
            upnl    = cur_val - cost
            upnl_pct = (upnl / cost * 100) if cost else 0.0
            edate = (str(row.get("entry_date", "") or "").strip() or None) if has_date_col else None
            edate = edate or entry_dates.get(symbol)

            holdings.append(LiveHolding(
                ticker=ticker, symbol=symbol,
                sector=sector_map.get(ticker, "DIVERSIFIED"),
                quantity=qty,
                avg_price=round(avg_p,  2),
                current_price=round(last_p, 2),
                current_value=round(cur_val, 2),
                cost_basis=round(cost, 2),
                unrealized_pnl=round(upnl, 2),
                unrealized_pnl_pct=round(upnl_pct, 2),
                allocation_pct=0.0,
                stop_price=stops.get(symbol),
                entry_date=edate,
            ))
            holdings_value += cur_val

        for h in holdings:
            h.allocation_pct = round(h.current_value / capital * 100, 2) if capital else 0.0

        available_cash = max(0.0, capital - holdings_value)
        net = holdings_value + available_cash
        return PortfolioState(
            as_of=datetime.now().isoformat(timespec="seconds"),
            source="MANUAL_CSV",
            total_capital=capital,
            holdings_value=round(holdings_value, 2),
            available_cash=round(available_cash, 2),
            net_equity=round(net, 2),
            deployed_pct=round(holdings_value / capital * 100, 2) if capital else 0.0,
            cash_pct=round(available_cash / capital * 100, 2) if capital else 0.0,
            holdings=holdings, open_orders=[],
            unrealized_pnl=round(sum(h.unrealized_pnl for h in holdings), 2),
        )

    # ── C) Simulated from last portfolio snapshot ────────────────────────────
    @staticmethod
    def from_simulation(capital: float) -> PortfolioState:
        """
        Reconstruct positions from the most recent PortfolioSnapshot JSON.
        Uses entry prices as cost basis — no live prices, no cash data.
        Always succeeds (returns an empty book if no history exists).
        """
        files = sorted(_PORTFOLIO_HIST.glob("*.json")) if _PORTFOLIO_HIST.exists() else []
        sector_map  = _load_sector_map()
        stops       = _load_portfolio_stops()
        entry_dates = _find_entry_dates()

        if not files:
            return _empty_state(capital, "SIMULATED (no history)")

        try:
            data = json.loads(files[-1].read_text(encoding="utf-8"))
        except Exception:
            return _empty_state(capital, "SIMULATED (unreadable history)")

        holdings: list[LiveHolding] = []
        holdings_value = 0.0

        for p in data.get("positions", []):
            sym    = str(p.get("ticker", "")).upper()
            ticker = sym if sym.endswith(".NS") else f"{sym}.NS"
            symbol = sym[:-3] if sym.endswith(".NS") else sym
            alloc  = float(p.get("allocation", 0) or 0)
            entry  = float(p.get("entry", 0) or 0)
            stop   = float(p["stop"]) if p.get("stop") else stops.get(symbol)

            tgt_val = alloc / 100.0 * capital
            qty     = int(tgt_val / entry) if entry > 0 else 0
            if qty <= 0:
                continue
            val = qty * entry

            holdings.append(LiveHolding(
                ticker=ticker, symbol=symbol,
                sector=sector_map.get(ticker, p.get("sector", "DIVERSIFIED")),
                quantity=qty,
                avg_price=round(entry, 2),
                current_price=round(entry, 2),   # no live price available
                current_value=round(val, 2),
                cost_basis=round(val, 2),
                unrealized_pnl=0.0, unrealized_pnl_pct=0.0,
                allocation_pct=round(val / capital * 100, 2) if capital else 0.0,
                stop_price=round(stop, 2) if stop else None,
                entry_date=entry_dates.get(symbol),
            ))
            holdings_value += val

        available_cash = max(0.0, capital - holdings_value)
        net = holdings_value + available_cash
        return PortfolioState(
            as_of=datetime.now().isoformat(timespec="seconds"),
            source="SIMULATED (last portfolio snapshot)",
            total_capital=capital,
            holdings_value=round(holdings_value, 2),
            available_cash=round(available_cash, 2),
            net_equity=round(net, 2),
            deployed_pct=round(holdings_value / capital * 100, 2) if capital else 0.0,
            cash_pct=round(available_cash / capital * 100, 2) if capital else 0.0,
            holdings=holdings, open_orders=[], unrealized_pnl=0.0,
        )


# ─────────────────────────────────────────────────────────────────────────────
# MODULE-LEVEL API
# ─────────────────────────────────────────────────────────────────────────────

def load_state(capital: float) -> PortfolioState:
    """
    Best-available state (priority order):
      1. Zerodha live   — if KiteConnect session is valid
      2. Manual CSV     — if state/holdings.csv exists
      3. Cached state   — if state/portfolio_state.json was written today
      4. Simulated      — last PortfolioSnapshot (always succeeds)
    """
    state = StateLoader.from_kite(capital)
    if state is not None:
        return state

    state = StateLoader.from_csv(capital)
    if state is not None:
        return state

    state = PortfolioState.load()
    if state is not None:
        state.total_capital = capital           # update capital in case it changed
        # recompute ratios
        state.deployed_pct = round(state.holdings_value / capital * 100, 2) if capital else 0.0
        state.cash_pct = round(state.available_cash / capital * 100, 2) if capital else 0.0
        for h in state.holdings:
            h.allocation_pct = round(h.current_value / capital * 100, 2) if capital else 0.0
        _log.warning("Using cached portfolio state from %s — run --sync-portfolio to refresh", state.as_of)
        return state

    _log.warning("No live or cached state found — falling back to simulated portfolio.")
    return StateLoader.from_simulation(capital)


def sync_and_save(capital: float) -> PortfolioState:
    """
    Force-sync from the best available source and persist to state/portfolio_state.json.
    Called by --sync-portfolio.
    """
    state = StateLoader.from_kite(capital)
    if state is None:
        state = StateLoader.from_csv(capital)
    if state is None:
        state = StateLoader.from_simulation(capital)
    path = state.save()
    return state, path


def state_to_lifecycle_holdings(state: PortfolioState) -> list:
    """Convert PortfolioState.holdings → list[Holding] for LifecycleManager.review()."""
    from portfolio.lifecycle_engine import Holding
    return [
        Holding(
            ticker=h.ticker, symbol=h.symbol, sector=h.sector,
            allocation=h.allocation_pct,
            entry_price=h.avg_price,
            stop_price=h.stop_price,
            prior_conviction=None,
            entry_date=h.entry_date,
        )
        for h in state.holdings
    ]


# ─────────────────────────────────────────────────────────────────────────────
# PRIVATE
# ─────────────────────────────────────────────────────────────────────────────

def _empty_state(capital: float, source: str) -> PortfolioState:
    return PortfolioState(
        as_of=datetime.now().isoformat(timespec="seconds"),
        source=source,
        total_capital=capital,
        holdings_value=0.0,
        available_cash=float(capital),
        net_equity=float(capital),
        deployed_pct=0.0,
        cash_pct=100.0,
        holdings=[], open_orders=[], unrealized_pnl=0.0,
    )


# ─────────────────────────────────────────────────────────────────────────────
# DEPLOYMENT CONFIG LOADER
# ─────────────────────────────────────────────────────────────────────────────

_DEPLOYMENT_CONFIG = Path("config/deployment.yaml")
_DEFAULT_DEPLOY = {
    "capital":                500_000,
    "risk_per_trade_pct":     1.0,
    "target_positions":       5,
    "max_new_per_week":       3,
    "time_stop_weeks":        12,
    "posture":                "paper",
    "min_trade_value":        5_000,
    "rebalance_threshold_pct": 2.0,
}


def load_deployment_config() -> dict:
    """Read config/deployment.yaml, fall back to built-in defaults."""
    if not _DEPLOYMENT_CONFIG.exists():
        return dict(_DEFAULT_DEPLOY)
    try:
        text = _DEPLOYMENT_CONFIG.read_text(encoding="utf-8")
        try:
            import yaml  # type: ignore
            d = yaml.safe_load(text) or {}
        except ImportError:
            d = _parse_yaml_simple(text)
        merged = dict(_DEFAULT_DEPLOY)
        merged.update({k: v for k, v in d.items() if k in _DEFAULT_DEPLOY})
        return merged
    except Exception as exc:
        _log.warning("deployment.yaml load failed: %s — using defaults", exc)
        return dict(_DEFAULT_DEPLOY)


def _parse_yaml_simple(text: str) -> dict:
    """Minimal key: value YAML parser (single-level, no lists needed here)."""
    out = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if not k:
            continue
        if v.lower() in ("true", "yes"):
            out[k] = True
        elif v.lower() in ("false", "no"):
            out[k] = False
        else:
            try:
                out[k] = int(v)
            except ValueError:
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = v.strip('"\'')
    return out
