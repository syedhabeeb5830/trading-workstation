"""
scanner/state_store.py — Persistent scan-state cache.

Survives restarts, Ctrl+C, and power failures via atomic JSON writes.

What is persisted per ticker:
  status       — last known READY / WATCH / EXTENDED / GAPPED / AVOID
  prev_status  — the status before the most recent update
  first_seen   — ISO timestamp of the first scan that produced this entry
  last_seen    — ISO timestamp of the most recent scan update
  entry_price  — from the most recent trade plan
  stop_price   — from the most recent trade plan
  grade        — A+ / A / B / AVOID
  score        — composite score
  last_alert   — ISO timestamp of the most recent TRIGGER alert for this ticker

Purpose:
  1. WATCH → READY transition detection across restarts.
     The alerts engine normally detects this by diffing today vs. yesterday in
     the scan log.  The state store provides a redundant, always-current view
     that survives a mid-day crash.

  2. Duplicate-alert suppression.
     ``was_alerted_today(ticker)`` returns True if a TRIGGER alert was already
     sent today, preventing re-alerts when ``--today`` is run multiple times.

  3. Audit visibility.
     Operators can inspect Journal/scan_state.json at any time to see the
     last-known status of every watched ticker without re-running the scanner.

Usage (optional integration — does not change CLI behaviour if unused):

    from scanner.state_store import StateStore
    store = StateStore()
    store.update("RELIANCE.NS", "READY", plan)
    store.mark_alerted("RELIANCE.NS")
    store.save()
    # restart…
    store2 = StateStore()
    state  = store2.get("RELIANCE.NS")
    # state["prev_status"] == "WATCH"  (preserved across restart)
"""

from __future__ import annotations

import contextlib
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional


STATE_FILENAME = "scan_state.json"
_SCHEMA_VERSION = 1


class StateStore:
    """
    Lightweight JSON-backed state cache for scanner READY/WATCH/OPEN/ALERTED.

    Thread-safety: single-writer; no locking needed for the CLI model.
    """

    def __init__(self, journal_dir: str = "Journal") -> None:
        self._path = Path(journal_dir) / STATE_FILENAME
        self._data: dict = self._load()

    # ── I/O ──────────────────────────────────────────────────────────────────

    def _load(self) -> dict:
        if not self._path.exists():
            return {"version": _SCHEMA_VERSION, "states": {}}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("bad root type")
            return raw
        except (json.JSONDecodeError, OSError, ValueError):
            # Corrupt or missing — start fresh; don't crash
            return {"version": _SCHEMA_VERSION, "states": {}}

    def save(self) -> None:
        """Atomically persist current state to disk."""
        self._data["saved_at"] = datetime.now().isoformat(timespec="seconds")
        self._data["version"]  = _SCHEMA_VERSION
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        try:
            content = json.dumps(self._data, indent=2, default=str)
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(str(tmp), str(self._path))
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise

    # ── Per-ticker API ────────────────────────────────────────────────────────

    def get(self, ticker: str) -> Optional[dict]:
        """Return the persisted state dict for *ticker*, or None."""
        return self._data.get("states", {}).get(ticker)

    def update(self, ticker: str, status: str, plan: Optional[dict] = None) -> None:
        """
        Record the latest scan result for *ticker*.

        *prev_status* is automatically carried forward from the previous call so
        callers can reconstruct WATCH→READY transitions even after a restart.
        """
        states = self._data.setdefault("states", {})
        prev   = states.get(ticker, {})
        now    = datetime.now().isoformat(timespec="seconds")
        entry  = {
            "status":      status,
            "prev_status": prev.get("status"),
            "first_seen":  prev.get("first_seen") or now,
            "last_seen":   now,
            "entry_price": (plan or {}).get("entry_price"),
            "stop_price":  (plan or {}).get("stop_price"),
            "grade":       (plan or {}).get("grade"),
            "score":       (plan or {}).get("score"),
        }
        # Preserve last_alert across updates
        if prev.get("last_alert"):
            entry["last_alert"] = prev["last_alert"]
        states[ticker] = entry

    def mark_alerted(self, ticker: str) -> None:
        """Record that a TRIGGER alert was sent for *ticker* right now."""
        states = self._data.setdefault("states", {})
        if ticker not in states:
            states[ticker] = {
                "status": None, "prev_status": None,
                "first_seen": None, "last_seen": None,
            }
        states[ticker]["last_alert"] = datetime.now().isoformat(timespec="seconds")

    def was_alerted_today(self, ticker: str) -> bool:
        """
        Return True if a TRIGGER alert was already sent for *ticker* today.
        Used to suppress duplicate alerts within the same trading day.
        """
        entry = (self._data.get("states") or {}).get(ticker) or {}
        last  = entry.get("last_alert")
        if not last:
            return False
        try:
            alert_date = datetime.fromisoformat(last).date()
        except (ValueError, TypeError):
            return False
        return alert_date == date.today()

    def get_all_states(self) -> dict[str, dict]:
        """Return the full ticker → state mapping."""
        return dict(self._data.get("states") or {})

    def purge_stale(self, max_age_days: int = 30) -> int:
        """
        Remove state entries not updated in *max_age_days* days.
        Returns the number of entries removed.
        """
        states = self._data.get("states") or {}
        cutoff = datetime.now().timestamp() - max_age_days * 86_400
        to_del = []
        for ticker, entry in states.items():
            last = entry.get("last_seen")
            try:
                ts = datetime.fromisoformat(last).timestamp() if last else 0.0
            except (ValueError, TypeError):
                ts = 0.0
            if ts < cutoff:
                to_del.append(ticker)
        for t in to_del:
            del states[t]
        return len(to_del)