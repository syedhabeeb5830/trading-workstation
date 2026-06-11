"""
analytics/setup_store.py — Setup Lifecycle Manager
===================================================
Tracks unique (strategy, ticker) setups across scan days.

Core invariant: at most ONE active setup per (strategy, ticker) at any time.
Daily scans UPDATE the existing setup rather than creating a duplicate.

Schema per setup:
  setup_id     : "{strategy}_{ticker}_{first_seen}" — human-readable, unique
  ticker       : NSE symbol (e.g. RELIANCE.NS)
  strategy     : strategy name (default "SWING")
  first_seen   : YYYY-MM-DD — date setup first appeared in scanner output
  last_seen    : YYYY-MM-DD — most recent scan date that touched this setup
  entry        : planned entry price (most recent scan)
  stop         : planned stop price (most recent scan)
  status       : current lifecycle status (see below)
  days_active  : trading days from first_seen to last_seen
  resolution   : exit reason when status is terminal

Status lifecycle:
  WATCH     → price not yet at entry level
  READY     → price within entry zone (1.5% of entry)
  TRIGGERED → trade was entered / GTT placed
  REJECTED  → setup scored below threshold (AVOID/GAPPED)
  EXPIRED   → setup exceeded expiry_days without triggering
  CLOSED    → trade completed (T1/T2/STOP resolved)

Allowed transitions:
  WATCH  → READY | REJECTED | EXPIRED
  READY  → TRIGGERED | REJECTED | EXPIRED
  TRIGGERED → CLOSED
  REJECTED / EXPIRED / CLOSED → terminal (no further transitions)

Persistence:
  Journal/setup_store.json — atomic write (temp → rename)
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np


# ── Constants ─────────────────────────────────────────────────────────────────

VALID_STATUSES = frozenset({
    "WATCH", "READY", "TRIGGERED", "REJECTED", "EXPIRED", "CLOSED",
})

ACTIVE_STATUSES = frozenset({"WATCH", "READY"})

DEFAULT_EXPIRY_DAYS = 7      # configurable via expire_stale_setups(expiry_days=N)

# Map raw scanner status → setup lifecycle status
_SCANNER_TO_SETUP: dict[str, str] = {
    "READY":    "READY",
    "WATCH":    "WATCH",
    "EXTENDED": "WATCH",    # still the same setup — price ran past entry temporarily
    "AVOID":    "REJECTED",
    "GAPPED":   "REJECTED",
    "BLOCKED":  "REJECTED",
    "OPEN":     "TRIGGERED",
}

# Status rank for upgrade-only transitions (WATCH < READY)
_STATUS_RANK = {"WATCH": 0, "READY": 1}


# ── Helper ────────────────────────────────────────────────────────────────────

def _trading_days_between(start: str, end: str) -> int:
    """
    Count trading days (Mon–Fri) from start to end inclusive.
    Returns 1 if dates are equal. Returns 0 on any parse error.
    """
    try:
        d1 = np.datetime64(start, "D")
        d2 = np.datetime64(end,   "D")
        n  = int(np.busday_count(d1, d2))
        return max(1, n + 1)
    except Exception:
        return 1


# ── SetupStore ────────────────────────────────────────────────────────────────

class SetupStore:
    """
    Manages unique (strategy, ticker) setup lifecycle records.

    Usage:
        store = SetupStore("Journal")
        sid, created = store.upsert_setup(
            ticker="RELIANCE.NS", strategy="SWING",
            entry=2500.0, stop=2450.0,
            scan_status="READY", scan_date="2026-06-05",
        )
        store.expire_stale_setups(today="2026-06-14")
        store.save()
    """

    def __init__(self, journal_dir: str = "journal") -> None:
        self._path  = Path(journal_dir) / "setup_store.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)

        self._setups: dict[str, dict]  = {}    # setup_id → setup dict
        self._active_index: dict[str, str] = {}  # "strategy|ticker" → setup_id
        self._dedup_count: int = 0              # cumulative duplicates prevented
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._setups      = data.get("setups", {})
            self._dedup_count = int(data.get("dedup_count", 0))
            self._rebuild_index()
        except Exception:
            pass

    def _rebuild_index(self) -> None:
        self._active_index = {}
        for sid, s in self._setups.items():
            if s.get("status") in ACTIVE_STATUSES:
                key = f"{s['strategy']}|{s['ticker']}"
                self._active_index[key] = sid

    def save(self) -> None:
        """Atomic write: write to .tmp then rename to avoid corruption."""
        tmp = self._path.with_suffix(".tmp")
        payload = json.dumps(
            {
                "saved_at":    datetime.now().isoformat(timespec="seconds"),
                "dedup_count": self._dedup_count,
                "setups":      self._setups,
            },
            indent=2,
            default=str,
        )
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self._path)

    # ── Core API ──────────────────────────────────────────────────────────────

    def upsert_setup(
        self,
        ticker:      str,
        strategy:    str,
        entry:       float,
        stop:        float,
        scan_status: str,
        scan_date:   str,
    ) -> tuple[str, bool]:
        """
        Create or update the setup for (strategy, ticker).

        Returns:
            (setup_id, created)
            created=True  → new setup was created
            created=False → existing setup was updated (dedup applied)

        Rules:
          - If an active (WATCH/READY) setup exists for (strategy, ticker),
            update it: last_seen, entry/stop, days_active, and optionally status.
          - Status can only upgrade WATCH→READY, never downgrade.
          - If scanner reports REJECTED for an active setup, close it.
          - If no active setup exists, create a new one.
        """
        new_status = _SCANNER_TO_SETUP.get(scan_status.upper().strip(), "WATCH")
        key        = f"{strategy}|{ticker}"

        # ── Update existing active setup ────────────────────────────────────
        if key in self._active_index:
            sid = self._active_index[key]
            s   = self._setups[sid]
            self._dedup_count += 1

            # Upgrade-only: WATCH → READY but never READY → WATCH
            if new_status in _STATUS_RANK and s["status"] in _STATUS_RANK:
                if _STATUS_RANK[new_status] > _STATUS_RANK[s["status"]]:
                    s["status"] = new_status
            elif new_status not in _STATUS_RANK:
                # Terminal transition (REJECTED)
                s["status"]     = new_status
                s["resolution"] = scan_status
                del self._active_index[key]

            s["last_seen"]   = scan_date
            s["entry"]       = round(float(entry), 2)
            s["stop"]        = round(float(stop),  2)
            s["days_active"] = _trading_days_between(s["first_seen"], scan_date)

            self._setups[sid] = s
            return sid, False

        # ── Create new setup ─────────────────────────────────────────────────
        base_id = f"{strategy}_{ticker}_{scan_date}".replace(".", "_")
        sid     = base_id
        counter = 1
        while sid in self._setups:          # ensure uniqueness on same-day re-runs
            sid     = f"{base_id}_{counter}"
            counter += 1

        s = {
            "setup_id":   sid,
            "ticker":     ticker,
            "strategy":   strategy,
            "first_seen": scan_date,
            "last_seen":  scan_date,
            "entry":      round(float(entry), 2),
            "stop":       round(float(stop),  2),
            "status":     new_status,
            "days_active": 1,
            "resolution": "",
        }
        self._setups[sid] = s

        if new_status in ACTIVE_STATUSES:
            self._active_index[key] = sid

        return sid, True

    def expire_stale_setups(
        self,
        today:        str,
        expiry_days:  int = DEFAULT_EXPIRY_DAYS,
    ) -> list[str]:
        """
        Mark all WATCH/READY setups as EXPIRED if their age exceeds expiry_days.

        Args:
            today       : current date YYYY-MM-DD
            expiry_days : max trading days an active setup can stay open

        Returns:
            List of setup_ids that were expired.
        """
        expired_ids: list[str] = []

        for sid, s in list(self._setups.items()):
            if s.get("status") not in ACTIVE_STATUSES:
                continue
            age = _trading_days_between(s["first_seen"], today)
            if age > expiry_days:
                s["status"]      = "EXPIRED"
                s["resolution"]  = f"stale after {age} trading days"
                s["last_seen"]   = today
                s["days_active"] = age
                self._setups[sid] = s
                key = f"{s['strategy']}|{s['ticker']}"
                self._active_index.pop(key, None)
                expired_ids.append(sid)

        return expired_ids

    def mark_triggered(
        self,
        ticker:   str,
        strategy: str = "SWING",
    ) -> Optional[str]:
        """
        Mark the active setup for (strategy, ticker) as TRIGGERED.
        Called when a GTT is placed via --place.

        Returns setup_id if found, None otherwise.
        """
        key = f"{strategy}|{ticker}"
        sid = self._active_index.pop(key, None)
        if sid and sid in self._setups:
            self._setups[sid]["status"] = "TRIGGERED"
            return sid
        return None

    def mark_closed(self, setup_id: str, resolution: str = "") -> None:
        """
        Mark a TRIGGERED setup as CLOSED (trade outcome resolved).

        Args:
            setup_id   : the setup to close
            resolution : e.g. "T1_HIT", "T2_HIT", "STOP_HIT"
        """
        if setup_id in self._setups:
            self._setups[setup_id]["status"]     = "CLOSED"
            self._setups[setup_id]["resolution"] = resolution

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_active_setups(self) -> list[dict]:
        """All WATCH/READY setups."""
        return [s.copy() for s in self._setups.values() if s.get("status") in ACTIVE_STATUSES]

    def get_all_setups(self) -> list[dict]:
        """All setups regardless of status."""
        return [s.copy() for s in self._setups.values()]

    def get_setup(self, setup_id: str) -> Optional[dict]:
        s = self._setups.get(setup_id)
        return s.copy() if s else None

    def get_active_setup_for(self, ticker: str, strategy: str = "SWING") -> Optional[dict]:
        """Return the active setup for (strategy, ticker), or None."""
        key = f"{strategy}|{ticker}"
        sid = self._active_index.get(key)
        return self.get_setup(sid) if sid else None

    # ── Stats ─────────────────────────────────────────────────────────────────

    def get_lifecycle_stats(self) -> dict:
        """
        Returns aggregate statistics for the lifecycle report.

        Keys returned:
            total, active, expired, triggered, closed, rejected,
            avg_duration_days, dedup_prevented, by_status,
            setups_by_ticker (list of active setups sorted by first_seen)
        """
        all_s = list(self._setups.values())

        by_status: dict[str, int] = {}
        durations: list[float]    = []

        for s in all_s:
            st = s.get("status", "UNKNOWN")
            by_status[st] = by_status.get(st, 0) + 1
            d = s.get("days_active", 1)
            if isinstance(d, (int, float)) and d >= 1:
                durations.append(float(d))

        avg_dur = round(sum(durations) / len(durations), 1) if durations else 0.0

        active_setups = sorted(
            self.get_active_setups(),
            key=lambda x: x.get("first_seen", ""),
        )

        return {
            "total":            len(all_s),
            "active":           by_status.get("WATCH", 0) + by_status.get("READY", 0),
            "expired":          by_status.get("EXPIRED", 0),
            "triggered":        by_status.get("TRIGGERED", 0),
            "closed":           by_status.get("CLOSED", 0),
            "rejected":         by_status.get("REJECTED", 0),
            "avg_duration_days": avg_dur,
            "dedup_prevented":  self._dedup_count,
            "by_status":        by_status,
            "active_setups":    active_setups,
        }
