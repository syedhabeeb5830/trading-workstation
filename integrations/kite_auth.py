"""
integrations/kite_auth.py — Automated Kite Session + Fail-Safe Freshness Layer
==============================================================================
Two responsibilities, both about NOT trading on stale/absent broker state:

  1. AUTO-LOGIN (pyotp)
     Kite Connect has no headless OAuth — the public API needs a `request_token`
     that normally comes from a browser redirect. This module performs the
     community-standard Kite *web* login programmatically (user_id + password →
     TOTP 2FA via pyotp → capture request_token → KiteConnect.generate_session),
     so a scheduler can refresh the daily session at ~08:45 IST with no human.

  2. FAIL-SAFE FRESHNESS (`@enforce_live_sync`)
     A swing cockpit on 3-day-old portfolio data places phantom orders. Any
     function that touches REAL money must run only when the broker sync is
     fresh. `@enforce_live_sync(max_age_minutes=15)` raises StaleSessionError
     (caught at the CLI boundary → hard stop) when the last sync is too old.

Design rules (consistent with the rest of the workstation):
  • Lazy imports for kiteconnect / pyotp / requests — the workstation must import
    and run research commands even when these broker deps are absent.
  • The session file format is OWNED by integrations/zerodha.py — we reuse its
    _save_session / _load_session / _get_credentials so there is ONE source of
    truth. We add only a separate sync HEARTBEAT (state/last_sync.json).
  • logging, never print. The CLI layer renders; the engine reports.
  • Never logs or persists password / api_secret / TOTP secret.

Environment (.env) keys consumed by auto_login():
    KITE_API_KEY, KITE_API_SECRET     (already used by zerodha.py)
    KITE_USER_ID                      (e.g. ZTJ763)
    KITE_PASSWORD                     (Kite web login password)
    KITE_TOTP_SECRET                  (the base32 secret behind your authenticator)
"""

from __future__ import annotations

import functools
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional, TypeVar
from urllib.parse import parse_qs, urlparse

_log = logging.getLogger(__name__)

# Heartbeat written every time we successfully pull live broker state. The
# freshness gate reads this — it is the authoritative "how long since we last
# saw the real account" timestamp (finer-grained than the daily session file).
_HEARTBEAT_FILE = Path("state/last_sync.json")
_STATE_FILE     = Path("state/portfolio_state.json")   # fallback freshness source

# Kite web-login endpoints (NOT the Connect API — these drive the browser flow).
_KITE_LOGIN_URL = "https://kite.zerodha.com/api/login"
_KITE_TWOFA_URL = "https://kite.zerodha.com/api/twofa"

DEFAULT_MAX_AGE_MINUTES = 15

F = TypeVar("F", bound=Callable[..., object])


# ─────────────────────────────────────────────────────────────────────────────
# EXCEPTIONS
# ─────────────────────────────────────────────────────────────────────────────
class StaleSessionError(RuntimeError):
    """Raised when an execution path runs against broker state older than the
    allowed freshness window. The caller MUST NOT place/modify orders."""


class AutoLoginError(RuntimeError):
    """Raised when the automated TOTP login flow cannot complete."""


# ─────────────────────────────────────────────────────────────────────────────
# SYNC HEARTBEAT
# ─────────────────────────────────────────────────────────────────────────────
def mark_synced(source: str = "kite", *, when: Optional[datetime] = None) -> datetime:
    """Record 'we just saw the real account at T'. Call this from every routine
    that pulls live holdings/cash (auto_login + --sync-portfolio + screen live
    path). Returns the timestamp written."""
    ts = when or datetime.now()
    try:
        _HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _HEARTBEAT_FILE.write_text(
            json.dumps({"synced_at": ts.isoformat(timespec="seconds"),
                        "source": source}, indent=2),
            encoding="utf-8")
    except OSError as exc:
        _log.warning("Could not write sync heartbeat: %s", exc)
    return ts


def _read_timestamp(path: Path, *keys: str) -> Optional[datetime]:
    """Best-effort read of an ISO timestamp under any of `keys` in a JSON file."""
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    for k in keys:
        raw = d.get(k)
        if raw:
            try:
                return datetime.fromisoformat(str(raw))
            except ValueError:
                continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# FRESHNESS
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SessionFreshness:
    is_fresh:       bool
    age_seconds:    Optional[float]   # None when no sync timestamp exists at all
    max_age_seconds: float
    session_valid:  bool              # a Kite session exists AND is dated today
    source:         str               # which timestamp drove the verdict
    reason:         str

    @property
    def age_minutes(self) -> Optional[float]:
        return None if self.age_seconds is None else round(self.age_seconds / 60.0, 2)


def freshness(max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES) -> SessionFreshness:
    """Is the broker state fresh enough to act on?

    Fresh ⇔ a same-day Kite session exists AND the last live sync (heartbeat,
    else portfolio_state.json `as_of`, else session `saved_at`) is within the
    window. The session-valid check alone is too coarse (it is day-granular and
    a transfer/fill can change the account mid-session)."""
    max_age = float(max_age_minutes) * 60.0

    session_valid = _session_is_today()

    ts = (_read_timestamp(_HEARTBEAT_FILE, "synced_at")
          or _read_timestamp(_STATE_FILE, "as_of")
          or _session_saved_at())
    src = ("heartbeat" if _HEARTBEAT_FILE.exists()
           else "portfolio_state" if _STATE_FILE.exists()
           else "session")

    if ts is None:
        return SessionFreshness(
            is_fresh=False, age_seconds=None, max_age_seconds=max_age,
            session_valid=session_valid, source="none",
            reason="no sync timestamp found — run --sync-portfolio / --kite-login")

    age = (datetime.now() - ts).total_seconds()
    if not session_valid:
        return SessionFreshness(
            is_fresh=False, age_seconds=age, max_age_seconds=max_age,
            session_valid=False, source=src,
            reason="Kite session missing or not dated today — run --kite-login")
    if age > max_age:
        return SessionFreshness(
            is_fresh=False, age_seconds=age, max_age_seconds=max_age,
            session_valid=True, source=src,
            reason=(f"last live sync {age/60:.1f} min ago (> {max_age_minutes:.0f} min) "
                    f"— refresh with --sync-portfolio"))
    return SessionFreshness(
        is_fresh=True, age_seconds=age, max_age_seconds=max_age,
        session_valid=True, source=src,
        reason=f"live sync {age/60:.1f} min ago (< {max_age_minutes:.0f} min)")


def _session_is_today() -> bool:
    try:
        from integrations import zerodha
        sess = zerodha._load_session()       # already enforces date==today
        return sess is not None
    except Exception:
        return False


def _session_saved_at() -> Optional[datetime]:
    try:
        from integrations import zerodha
        return _read_timestamp(zerodha.SESSION_FILE, "saved_at")
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# DECORATOR  —  the fail-safe gate
# ─────────────────────────────────────────────────────────────────────────────
def enforce_live_sync(max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES) -> Callable[[F], F]:
    """Decorator: block the wrapped function unless broker state is fresh.

    Usage:
        @enforce_live_sync(15)
        def place_protective_exit(...):  # touches real money → must be fresh
            ...

    Raises StaleSessionError (a hard stop the CLI converts to exit code 2) when
    the last live sync is older than `max_age_minutes`. Set the env override
    KITE_MAX_SYNC_AGE_MIN to change the window account-wide.
    """
    env_override = os.getenv("KITE_MAX_SYNC_AGE_MIN", "").strip()
    if env_override:
        try:
            max_age_minutes = float(env_override)
        except ValueError:
            _log.warning("Ignoring non-numeric KITE_MAX_SYNC_AGE_MIN=%r", env_override)

    def decorator(fn: F) -> F:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            fr = freshness(max_age_minutes)
            if not fr.is_fresh:
                _log.error("BLOCKED %s — stale broker state: %s", fn.__name__, fr.reason)
                raise StaleSessionError(
                    f"{fn.__name__}: refusing to execute — {fr.reason}")
            _log.info("%s — broker state fresh (%s)", fn.__name__, fr.reason)
            return fn(*args, **kwargs)
        return wrapper  # type: ignore[return-value]
    return decorator


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-LOGIN  (pyotp + Kite web flow)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class LoginResult:
    user_id:      str
    access_token: str
    synced_at:    str


def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise AutoLoginError(
            f"{name} missing from environment/.env — required for automated login")
    return val


def _lazy_requests():
    try:
        import requests  # type: ignore
        return requests
    except ImportError as exc:  # pragma: no cover
        raise AutoLoginError(
            "`requests` not installed — run: pip install requests") from exc


def _lazy_pyotp():
    try:
        import pyotp  # type: ignore
        return pyotp
    except ImportError as exc:  # pragma: no cover
        raise AutoLoginError(
            "`pyotp` not installed — run: pip install pyotp") from exc


def _capture_request_token(http_session, kite, api_key: str) -> str:
    """After 2FA the http_session holds the auth cookie. Hitting the Connect
    login URL 302-redirects to the app's redirect_url carrying request_token.
    The redirect target is often 127.0.0.1 (unreachable), so we walk Location
    headers manually instead of letting requests follow them."""
    requests = _lazy_requests()
    url = kite.login_url()
    for _ in range(10):
        try:
            resp = http_session.get(url, allow_redirects=False, timeout=10)
        except requests.exceptions.RequestException as exc:
            raise AutoLoginError(f"login redirect fetch failed: {exc}") from exc
        loc = resp.headers.get("Location", "")
        if "request_token=" in (loc or resp.url):
            target = loc or resp.url
            token = parse_qs(urlparse(target).query).get("request_token", [None])[0]
            if token:
                return token
        if not loc:
            break
        # Follow the next hop (relative or absolute)
        url = loc if loc.startswith("http") else f"https://kite.zerodha.com{loc}"
    raise AutoLoginError(
        "could not capture request_token from the login redirect — check that "
        f"KITE_REDIRECT_URL matches the app config (api_key={api_key[:4]}…)")


def auto_login(*, persist: bool = True) -> LoginResult:
    """Fully automated daily Kite login via stored credentials + TOTP.

    Steps: POST /api/login (user_id+password) → POST /api/twofa (pyotp TOTP) →
    capture request_token → KiteConnect.generate_session → persist via
    zerodha._save_session + write a sync heartbeat.

    Raises AutoLoginError on any failure (never silently half-logs-in).
    """
    from integrations import zerodha
    KiteConnect = zerodha._load_kite()
    zerodha._load_dotenv()

    api_key, api_secret, _ = zerodha._get_credentials()
    user_id     = _require("KITE_USER_ID")
    password    = _require("KITE_PASSWORD")
    totp_secret = _require("KITE_TOTP_SECRET")

    requests = _lazy_requests()
    pyotp    = _lazy_pyotp()
    http     = requests.Session()

    # Step 1 — password login → request_id
    try:
        r1 = http.post(_KITE_LOGIN_URL,
                       data={"user_id": user_id, "password": password}, timeout=10)
        r1.raise_for_status()
        request_id = r1.json()["data"]["request_id"]
    except Exception as exc:
        raise AutoLoginError(f"password login failed: {exc}") from exc

    # Step 2 — TOTP 2FA (generated locally; the secret never leaves this process)
    try:
        otp = pyotp.TOTP(totp_secret).now()
        r2 = http.post(_KITE_TWOFA_URL,
                       data={"user_id": user_id, "request_id": request_id,
                             "twofa_value": otp, "twofa_type": "totp"}, timeout=10)
        r2.raise_for_status()
    except Exception as exc:
        raise AutoLoginError(f"TOTP 2FA failed: {exc}") from exc

    # Step 3 — exchange the cookie-backed session for a request_token, then token
    kite = KiteConnect(api_key=api_key)
    request_token = _capture_request_token(http, kite, api_key)
    try:
        data = kite.generate_session(request_token, api_secret=api_secret)
    except Exception as exc:
        raise AutoLoginError(f"generate_session failed: {exc}") from exc

    access_token = data["access_token"]
    resolved_uid = data.get("user_id", user_id)

    if persist:
        zerodha._save_session(access_token, resolved_uid)
        ts = mark_synced(source="auto_login")
    else:
        ts = datetime.now()

    _log.info("Auto-login OK for %s (token cached, session dated %s)",
              resolved_uid, date.today().isoformat())
    return LoginResult(user_id=resolved_uid, access_token=access_token,
                       synced_at=ts.isoformat(timespec="seconds"))


def ensure_fresh_or_login(max_age_minutes: float = DEFAULT_MAX_AGE_MINUTES) -> SessionFreshness:
    """Convenience for schedulers: if state is stale, attempt a single auto-login,
    then re-check. Returns the final freshness (does NOT raise — the caller
    decides). Use the decorator on the actual execution functions for the hard
    stop."""
    fr = freshness(max_age_minutes)
    if fr.is_fresh:
        return fr
    try:
        auto_login()
    except AutoLoginError as exc:
        _log.error("Auto-login attempt failed: %s", exc)
        return freshness(max_age_minutes)
    return freshness(max_age_minutes)
