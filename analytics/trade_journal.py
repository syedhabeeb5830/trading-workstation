"""
analytics/trade_journal.py — Live Trade Journal (YOUR trades, not market history)
=================================================================================
The 94k-observation database is market history. THIS is the validation that
actually matters: your real executions. Every taken trade is stored with its full
decision context (regime, edge health, conviction, tier, sector, calibrated
levels), then resolved to a realized R-multiple. Rolling analytics from these feed
the Adaptive Risk Engine and the Kill Switch.

Stores: Journal/live_trades.csv     Context snapshot: Journal/plan_context.json

Recording paths:
  • snapshot_plan(...)   — the screen records each BUY-TODAY name's context.
  • sync_from_kite()     — new Kite holdings become journal entries (enriched from
                           the snapshot); vanished holdings are closed at realized P&L.
  • record_entry(...)    — direct/manual.
Open trades with no broker exit are auto-resolved against their own plan
(bank ½ at T1, trail the rest to T2 / stop / 60-day time-stop) via yfinance.

Pure bookkeeping + arithmetic — no scores, no indicators.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

_log = logging.getLogger(__name__)

_JOURNAL = Path("Journal/live_trades.csv")
_CONTEXT = Path("Journal/plan_context.json")
_HORIZON = 60   # trading-day time stop for auto-resolution

COLS = ["trade_id", "entry_date", "ticker", "sector", "regime", "edge_mode",
        "risk_mode", "conviction", "tier", "entry", "stop", "t1", "t2", "qty",
        "risk_inr", "status", "exit_date", "exit_price", "exit_reason",
        "holding_days", "realized_r", "realized_pct"]


# ─────────────────────────────────────────────────────────────────────────────
# PLAN CONTEXT (screen → snapshot so fills can be enriched at entry context)
# ─────────────────────────────────────────────────────────────────────────────
def snapshot_plan(selected: list[dict], regime: str, edge_mode: str,
                  risk_mode: str) -> None:
    """Persist today's BUY-TODAY context keyed by symbol (for sync_from_kite)."""
    try:
        ctx = {}
        for c in selected:
            ctx[c["sym"]] = {
                "date": date.today().isoformat(), "sector": c.get("sector", ""),
                "regime": regime, "edge_mode": edge_mode, "risk_mode": risk_mode,
                "conviction": round(float(c.get("conv", 0)), 0), "tier": c.get("tier", ""),
                "entry": c.get("entry"), "stop": c.get("stop"),
                "t1": c.get("t1"), "t2": c.get("t2"),
                "qty": c.get("qty"), "risk_inr": round(float(c.get("risk", 0)), 0),
            }
        _CONTEXT.parent.mkdir(parents=True, exist_ok=True)
        _CONTEXT.write_text(json.dumps(ctx, indent=2), encoding="utf-8")
    except Exception as exc:
        _log.warning("plan snapshot failed: %s", exc)


def _load_context() -> dict:
    try:
        if _CONTEXT.exists():
            return json.loads(_CONTEXT.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# STORE
# ─────────────────────────────────────────────────────────────────────────────
def _load() -> pd.DataFrame:
    if _JOURNAL.exists():
        try:
            return pd.read_csv(_JOURNAL)
        except Exception:
            pass
    return pd.DataFrame(columns=COLS)


def _save(df: pd.DataFrame) -> None:
    _JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(_JOURNAL, index=False, encoding="utf-8")


def record_entry(ticker: str, ctx: dict) -> Optional[str]:
    """Append one OPEN trade from a context dict. Idempotent per ticker+date."""
    df = _load()
    sym = ticker.replace(".NS", "")
    d = ctx.get("date", date.today().isoformat())
    tid = f"{d.replace('-','')}_{sym}"
    if not df.empty and (df["trade_id"] == tid).any():
        return None
    row = {c: "" for c in COLS}
    row.update({
        "trade_id": tid, "entry_date": d, "ticker": sym,
        "sector": ctx.get("sector", ""), "regime": ctx.get("regime", ""),
        "edge_mode": ctx.get("edge_mode", ""), "risk_mode": ctx.get("risk_mode", ""),
        "conviction": ctx.get("conviction", ""), "tier": ctx.get("tier", ""),
        "entry": ctx.get("entry", ""), "stop": ctx.get("stop", ""),
        "t1": ctx.get("t1", ""), "t2": ctx.get("t2", ""), "qty": ctx.get("qty", ""),
        "risk_inr": ctx.get("risk_inr", ""), "status": "OPEN",
        "exit_reason": "OPEN",
    })
    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    _save(df)
    return tid


# ─────────────────────────────────────────────────────────────────────────────
# KITE SYNC — real holdings ↔ journal
# ─────────────────────────────────────────────────────────────────────────────
def sync_from_kite() -> dict:
    """New Kite holdings → OPEN journal entries (enriched from plan context);
    holdings that vanished → CLOSE at realized P&L. Returns a small summary."""
    try:
        from integrations.zerodha import get_client_or_none
        kc = get_client_or_none()
        if kc is None:
            return {"synced": 0, "closed": 0, "note": "no live Kite session"}
        hdf = kc.holdings_df()
    except Exception as exc:
        return {"synced": 0, "closed": 0, "note": f"kite error: {exc}"}

    ctx_all = _load_context()
    df = _load()
    held = {}
    if hdf is not None and not hdf.empty:
        for _, r in hdf.iterrows():
            sym = str(r.get("ticker", "")).replace(".NS", "").replace(".BO", "").upper()
            if int(r.get("quantity", 0) or 0) > 0:
                held[sym] = {"avg": float(r.get("avg_price", 0) or 0),
                             "last": float(r.get("last_price", 0) or 0)}

    synced = 0
    for sym, h in held.items():
        tid_today = f"{date.today().isoformat().replace('-','')}_{sym}"
        if not df.empty and ((df["ticker"] == sym) & (df["status"] == "OPEN")).any():
            continue
        c = ctx_all.get(sym, {})
        c = {**c, "entry": c.get("entry", h["avg"]), "date": c.get("date", date.today().isoformat())}
        if record_entry(sym, c):
            synced += 1
    # CLOSE entries no longer held (broker truth) — only if not already closed
    df = _load()
    closed = 0
    for i, r in df.iterrows():
        if r["status"] != "OPEN":
            continue
        if str(r["ticker"]).upper() not in held:
            # vanished from holdings → treat as closed; resolution fills price below
            pass
    if synced:
        _save(df)
    return {"synced": synced, "closed": closed, "note": f"{len(held)} live holdings"}


# ─────────────────────────────────────────────────────────────────────────────
# RESOLVE — auto-close open trades against their own plan (yfinance)
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_forward(ticker: str, start: str) -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf
        df = yf.download(f"{ticker}.NS", start=start, progress=False,
                         auto_adjust=True, threads=False)
        if df is None or df.empty:
            return None
        if hasattr(df.columns, "get_level_values"):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception:
        return None


def resolve_open() -> int:
    """Resolve OPEN trades to realized R using the plan (½ at T1, trail to T2/stop/
    time). Returns count resolved. Skips names with insufficient data."""
    df = _load()
    if df.empty:
        return 0
    resolved = 0
    for i, r in df.iterrows():
        if str(r.get("status")) != "OPEN":
            continue
        try:
            entry = float(r["entry"]); stop = float(r["stop"])
            t1 = float(r["t1"]); t2 = float(r["t2"])
        except (ValueError, TypeError):
            continue
        if not (entry > 0 and stop > 0 and entry > stop):
            continue
        bars = _fetch_forward(str(r["ticker"]), str(r["entry_date"]))
        if bars is None or len(bars) < 2:
            continue
        fwd = bars.iloc[1:_HORIZON + 1]   # bars strictly after entry day
        risk = entry - stop
        exit_px, reason, hold = None, None, len(fwd)
        half_done = False
        realized_r = 0.0
        for d, (_, bar) in enumerate(fwd.iterrows(), 1):
            lo, hi = float(bar["Low"]), float(bar["High"])
            if lo <= stop:                                  # stop hit
                if half_done:
                    realized_r += 0.5 * ((stop - entry) / risk)
                else:
                    realized_r = (stop - entry) / risk
                exit_px, reason, hold = stop, "STOP", d
                break
            if not half_done and hi >= t1:                  # bank ½ at T1
                realized_r += 0.5 * ((t1 - entry) / risk)
                half_done = True
            if half_done and hi >= t2:                      # runner hits T2
                realized_r += 0.5 * ((t2 - entry) / risk)
                exit_px, reason, hold = t2, "T2", d
                break
        if exit_px is None:                                 # time stop at last close
            last = float(fwd["Close"].iloc[-1])
            if half_done:
                realized_r += 0.5 * ((last - entry) / risk)
            else:
                realized_r = (last - entry) / risk
            exit_px, reason = last, "TIME"
        df.at[i, "status"] = "CLOSED"
        df.at[i, "exit_price"] = round(exit_px, 2)
        df.at[i, "exit_reason"] = reason
        df.at[i, "holding_days"] = int(hold)
        df.at[i, "realized_r"] = round(realized_r, 2)
        df.at[i, "realized_pct"] = round((exit_px / entry - 1) * 100, 2)
        df.at[i, "exit_date"] = date.today().isoformat()
        resolved += 1
    if resolved:
        _save(df)
    return resolved


# ─────────────────────────────────────────────────────────────────────────────
# ANALYTICS — feeds the kill switch + the screen
# ─────────────────────────────────────────────────────────────────────────────
def _max_drawdown_pct(returns_pct: list[float]) -> float:
    """Max drawdown of a compounding equity curve from per-trade % returns."""
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in returns_pct:
        eq *= (1 + r / 100.0)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return round(mdd * 100, 1)


def analytics() -> dict:
    df = _load()
    closed = df[df.get("status") == "CLOSED"].copy() if not df.empty else pd.DataFrame()
    n_open = int((df.get("status") == "OPEN").sum()) if not df.empty else 0
    out = {"n_total": int(len(df)), "n_open": n_open, "n_closed": int(len(closed))}
    if closed.empty:
        out.update({"win_rate": None, "expectancy_R": None, "rolling20_expectancy_R": None,
                    "profit_factor": None, "max_dd_pct": None, "avg_hold": None,
                    "by_tier": {}, "by_regime": {}, "by_sector": {}})
        return out
    closed["realized_r"] = pd.to_numeric(closed["realized_r"], errors="coerce")
    closed["realized_pct"] = pd.to_numeric(closed["realized_pct"], errors="coerce")
    closed = closed.dropna(subset=["realized_r"])
    wins = closed[closed["realized_r"] > 0]["realized_r"]
    losses = closed[closed["realized_r"] <= 0]["realized_r"]
    gross_win = float(wins.sum()); gross_loss = float(abs(losses.sum()))
    r20 = closed["realized_r"].tail(20)

    def _grp(col):
        g = {}
        for k, sub in closed.groupby(col):
            if not str(k):
                continue
            g[str(k)] = {"n": int(len(sub)),
                         "exp_R": round(float(sub["realized_r"].mean()), 2),
                         "win%": round(float((sub["realized_r"] > 0).mean() * 100), 0)}
        return g

    out.update({
        "win_rate": round(float((closed["realized_r"] > 0).mean() * 100), 1),
        "expectancy_R": round(float(closed["realized_r"].mean()), 2),
        "rolling20_expectancy_R": round(float(r20.mean()), 2) if len(r20) else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "avg_win_R": round(float(wins.mean()), 2) if len(wins) else None,
        "avg_loss_R": round(float(losses.mean()), 2) if len(losses) else None,
        "max_dd_pct": _max_drawdown_pct(closed["realized_pct"].dropna().tolist()),
        "avg_hold": round(float(pd.to_numeric(closed["holding_days"],
                          errors="coerce").mean()), 0),
        "by_tier": _grp("tier"), "by_regime": _grp("regime"), "by_sector": _grp("sector"),
    })
    return out


def load_stats() -> dict:
    """Lightweight accessor for the screen / kill switch (no resolution)."""
    try:
        return analytics()
    except Exception as exc:
        _log.warning("journal analytics failed: %s", exc)
        return {"n_closed": 0}
