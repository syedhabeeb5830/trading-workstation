"""
scanner/reconcile.py — Journal ↔ Zerodha Reconciliation
========================================================
Pulls live holdings & positions from Zerodha and diffs them against the
local journal. Surfaces three kinds of drift:

  ORPHAN     in broker but NOT in journal      → "I bought without logging"
  MISSING    in journal but NOT in broker      → "Logged but never filled,
                                                  or already exited untracked"
  QTY MISMATCH same ticker, different quantity → "Partial fill or scaled out"

These are the three operational failure modes that destroy a journal's
truthfulness. Running --reconcile after each market session keeps the
analytics layer honest.
"""

from __future__ import annotations
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from analytics.outcome_tracker import load_trade_log
from integrations.zerodha     import KiteClient


# ── Colour helpers ───────────────────────────────────────────────────────────
_R, _Y, _G, _C, _D, _B, _RST = (
    "\033[91m", "\033[93m", "\033[92m",
    "\033[96m", "\033[2m",  "\033[1m", "\033[0m",
)
def _r(s): return f"{_R}{s}{_RST}"
def _y(s): return f"{_Y}{s}{_RST}"
def _g(s): return f"{_G}{s}{_RST}"
def _c(s): return f"{_C}{s}{_RST}"
def _d(s): return f"{_D}{s}{_RST}"
def _b(s): return f"{_B}{s}{_RST}"


# ─────────────────────────────────────────────────────────────────────────────
# SYNC — pull live snapshot from Kite and persist it
# ─────────────────────────────────────────────────────────────────────────────
def run_sync(journal_dir: str = "journal") -> dict:
    """Fetch holdings + positions from Kite, save to journal/zerodha_snapshot.json."""
    Path(journal_dir).mkdir(parents=True, exist_ok=True)
    snap_path = Path(journal_dir) / "zerodha_snapshot.json"

    print("\n  Fetching from Zerodha...")
    client = KiteClient()
    holdings_df  = client.holdings_df()
    positions_df = client.positions_df()

    snapshot = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "user_id":    client.user_id,
        "holdings":   holdings_df.to_dict(orient="records"),
        "positions":  positions_df.to_dict(orient="records"),
    }
    snap_path.write_text(json.dumps(snapshot, indent=2, default=str))

    print(f"  ✓ Synced  {len(holdings_df)} holdings, "
          f"{len(positions_df)} intraday positions")
    print(f"  ✓ Saved   {snap_path}\n")
    return snapshot


def load_snapshot(journal_dir: str = "journal") -> Optional[dict]:
    snap_path = Path(journal_dir) / "zerodha_snapshot.json"
    if not snap_path.exists():
        return None
    return json.loads(snap_path.read_text())


# ─────────────────────────────────────────────────────────────────────────────
# RECONCILE — diff journal vs broker
# ─────────────────────────────────────────────────────────────────────────────
def run_reconcile(journal_dir: str = "journal", auto_sync: bool = True) -> dict:
    """
    Compares open trades in journal/trades.csv against the latest Zerodha
    snapshot (or fetches a fresh one when auto_sync=True).
    """
    if auto_sync:
        run_sync(journal_dir)

    snap = load_snapshot(journal_dir)
    if snap is None:
        print("  ✗ No Zerodha snapshot. Run --sync first.")
        return {}

    holdings = pd.DataFrame(snap["holdings"])
    journal  = load_trade_log(journal_dir)
    open_j   = journal[
        (journal["resolved"].astype(str) != "True") &
        (journal["exit_reason"].astype(str).isin(["OPEN", "", "nan"]))
    ].copy()

    # Build lookup maps keyed by ticker
    j_map = {str(r["ticker"]): r for _, r in open_j.iterrows()}
    h_map = {str(r["ticker"]): r for _, r in holdings.iterrows()} if not holdings.empty else {}

    j_tickers = set(j_map.keys())
    h_tickers = set(h_map.keys())

    orphans  = sorted(h_tickers - j_tickers)
    missing  = sorted(j_tickers - h_tickers)
    common   = sorted(j_tickers & h_tickers)

    qty_mismatches = []
    for tk in common:
        j_qty = float(j_map[tk].get("quantity", 0) or 0)
        h_qty = float(h_map[tk].get("quantity", 0) or 0)
        if abs(j_qty - h_qty) > 0.5:   # tolerate float noise
            qty_mismatches.append((tk, j_qty, h_qty))

    # ── Render ──────────────────────────────────────────────────────────────
    print("\n" + "═" * 70)
    print(f"  {_b('RECONCILIATION')}  —  {snap['fetched_at']}  (Kite user {snap['user_id']})")
    print("═" * 70)
    print(f"  Journal open trades   : {len(open_j)}")
    print(f"  Broker holdings       : {len(holdings)}")
    print(f"  Matched cleanly       : {len(common) - len(qty_mismatches)}")

    if not orphans and not missing and not qty_mismatches:
        print(f"\n  {_g('✓ Journal and broker are in sync.')}\n")
        print("═" * 70 + "\n")
        return {"orphans": [], "missing": [], "qty_mismatches": []}

    if orphans:
        print(f"\n  {_y('⚠ ORPHANS')}  (in broker, not in journal — you traded without logging):")
        for tk in orphans:
            h = h_map[tk]
            print(f"    • {_c(tk):<20} {int(h['quantity'])} sh @ ₹{float(h['avg_price']):,.2f}")
        print(f"    {_d('Fix:')}  python run.py --record {orphans[0]}")

    if missing:
        print(f"\n  {_r('✗ MISSING')}  (in journal, not in broker — never filled or exited untracked):")
        for tk in missing:
            j = j_map[tk]
            print(f"    • {_c(tk):<20} logged {int(j['quantity'])} sh "
                  f"entry ₹{float(j['entry_price']):,.2f} on {str(j['entry_date'])[:10]}")
        print(f"    {_d('Fix:')}  python run.py --resolve   (will mark as MANUAL/exit)")

    if qty_mismatches:
        print(f"\n  {_y('⚠ QTY MISMATCH')}  (partial fills or untracked scale-outs):")
        for tk, jq, hq in qty_mismatches:
            print(f"    • {_c(tk):<20} journal {int(jq)} sh  ↔  broker {int(hq)} sh  "
                  f"(diff {int(hq - jq):+d})")
        print(f"    {_d('Fix:')}  edit journal/trades.csv quantity column manually")

    print("\n" + "═" * 70 + "\n")
    return {
        "orphans":        orphans,
        "missing":        missing,
        "qty_mismatches": qty_mismatches,
    }
