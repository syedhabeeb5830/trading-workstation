"""
scanner/morning.py — The Zero-Decision-Fatigue Morning Command
================================================================
  python run.py --morning

Output structure:
  1. Doctor (10 pre-flight checks)
  2. SCAN FUNNEL — every ticker, every rejection reason, nothing hidden
  3. QUALIFIED UNIVERSE — backtest stats + today's status for each
  4. TODAY''S DECISION — one answer with full context
"""

from __future__ import annotations
import json
from datetime import date, datetime
from pathlib import Path
from typing import Optional

# ── ANSI ─────────────────────────────────────────────────────────────────────
_R, _Y, _G, _C, _D, _B, _RST = (
    "\033[91m", "\033[93m", "\033[92m",
    "\033[96m", "\033[2m",  "\033[1m", "\033[0m",
)

_CACHE_MAX_AGE_DAYS = 30
_W = 72   # card width


# ─────────────────────────────────────────────────────────────────────────────
#  Data loaders
# ─────────────────────────────────────────────────────────────────────────────

def _load_qualified_universe(journal_dir: str = "Journal") -> tuple[set[str], dict]:
    """Returns (set_of_tickers, full_cache_dict). Empty set if missing/stale."""
    for candidate in (journal_dir, "Journal", "journal"):
        p = Path(candidate) / "qualified_universe.json"
        if p.exists():
            try:
                cache = json.loads(p.read_text())
                gen   = datetime.fromisoformat(cache.get("generated_at", ""))
                age_d = (datetime.now() - gen).days
                if age_d > _CACHE_MAX_AGE_DAYS:
                    return set(), {"_stale": True, "_age_d": age_d}
                tickers = {str(t).upper() for t in cache.get("qualified", [])}
                return tickers, cache
            except Exception:
                continue
    return set(), {}


def _load_todays_scan(journal_dir: str = "journal") -> list[dict]:
    try:
        from analytics.scan_logger import load_scan_log
        df = load_scan_log(journal_dir=journal_dir)
    except Exception:
        return []
    if df is None or df.empty:
        return []
    today = date.today().isoformat()
    if "scan_date" in df.columns:
        df = df[df["scan_date"].astype(str).str[:10] == today]
    if df.empty:
        return []
    return df.to_dict(orient="records")


def _load_open_positions(journal_dir: str = "journal") -> list[dict]:
    try:
        from analytics.outcome_tracker import load_trade_log
        df = load_trade_log(journal_dir=journal_dir)
    except Exception:
        return []
    if df is None or df.empty:
        return []
    open_df = df[df["status"].astype(str).str.upper() == "OPEN"]
    out = []
    for _, r in open_df.iterrows():
        entry = float(r.get("entry_price", 0) or 0)
        stop  = float(r.get("stop_price",  0) or 0)
        out.append({
            "ticker":         str(r.get("ticker", "")).upper(),
            "quantity":       float(r.get("quantity", 0) or 0),
            "risk_per_share": max(entry - stop, 0),
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
#  Print helpers
# ─────────────────────────────────────────────────────────────────────────────

def _divider(label: str = "", char: str = "-", width: int = _W) -> None:
    if label:
        side = max((width - len(label) - 2) // 2, 2)
        print(f"  {_D}{char * side} {label} {char * (width - side - len(label) - 2)}{_RST}")
    else:
        print(f"  {_D}{char * width}{_RST}")


def _row(cols: list, widths: list, colors: list = None) -> None:
    parts = []
    for i, (col, w) in enumerate(zip(cols, widths)):
        c = colors[i] if colors else ""
        rst = _RST if c else ""
        parts.append(f"{c}{str(col):<{w}}{rst}")
    print("  " + "  ".join(parts))


def _table_header(cols: list, widths: list) -> None:
    _row(cols, widths, [_D] * len(cols))
    print("  " + "  ".join(f"{_D}{'-' * w}{_RST}" for w in widths))


# ─────────────────────────────────────────────────────────────────────────────
#  Section printers
# ─────────────────────────────────────────────────────────────────────────────

_STATUS_COLOR = {"READY": _G, "WATCH": _Y, "EXTENDED": _Y, "AVOID": _R}
_GRADE_COLOR  = {"A+": _G, "A": _C, "B": _Y, "AVOID": _R}


def _print_scan_funnel(today_rows: list, qualified_set: set) -> dict:
    """Print full scan funnel. Returns dict ticker -> today_status."""
    today_status = {}

    _divider("SCAN FUNNEL  " + date.today().strftime("%b %d, %Y"))
    print()

    if not today_rows:
        print(f"  {_R}  No scan data for today. Run: python run.py --today{_RST}\n")
        return today_status

    from collections import Counter
    status_counts = Counter(str(r.get("status", "?")).upper() for r in today_rows)
    parts = []
    for s in ["READY", "WATCH", "EXTENDED", "AVOID"]:
        if s in status_counts:
            c = _STATUS_COLOR.get(s, _D)
            parts.append(f"{c}{s}{_RST} {status_counts[s]}")
    print(f"  {_D}Scanned:{_RST} {len(today_rows)} tickers   |   " + "  ".join(parts))
    print()

    for r in today_rows:
        tk = str(r.get("ticker", "")).upper()
        today_status[tk] = str(r.get("status", "?")).upper()

    ready_rows = [r for r in today_rows if str(r.get("status","")).upper() == "READY"]

    if not ready_rows:
        print(f"  {_Y}  No READY setups today.{_RST}\n")
        return today_status

    print(f"  {_D}READY setups — filter trace:{_RST}")
    print()
    col_w = [18, 7, 6, 8, 34]
    _table_header(["Ticker", "Grade", "Score", "RR", "Gate result"], col_w)

    for r in sorted(ready_rows, key=lambda x: float(x.get("score", 0) or 0), reverse=True):
        tk    = str(r.get("ticker", "")).upper()
        raw_grade = r.get("grade", None)
        grade_str = str(raw_grade).upper() if raw_grade and str(raw_grade).upper() not in ("", "NAN", "NONE", "?") else ""
        score = float(r.get("score", 0) or 0) if str(r.get("score", "")) not in ("", "nan", "NaN") else float("nan")
        rr    = float(r.get("rr_t1", 0) or 0)

        in_universe = tk in qualified_set
        grade_ok    = grade_str in {"A+", "A"}
        grade_nan   = not grade_str  # scanner ran but didn't compute grade

        if in_universe and (grade_ok or grade_nan):
            tag = "ungraded" if grade_nan else grade_str
            result = f"PASS  in validated universe ({tag})"
            rc = _G
        elif not in_universe and (grade_ok or grade_nan):
            result = "SKIP  not in validated universe"
            rc = _Y
        else:
            result = f"SKIP  grade {grade_str} (need A or A+)"
            rc = _R

        gc = _GRADE_COLOR.get(grade_str, "")
        score_s = f"{score:.0f}" if score == score else "-"  # NaN check
        _row([tk, grade_str or "-", score_s, f"{rr:.1f}x", result], col_w,
             [_G if in_universe else "", gc, "", _D, rc])

    print()
    return today_status


def _print_qualified_universe(qualified_set: set, cache: dict, today_status: dict) -> None:
    _divider("QUALIFIED UNIVERSE  (backtest-validated tickers only)")
    print()

    gen   = (cache or {}).get("generated_at", "?")
    days  = (cache or {}).get("days",         "?")
    stats = (cache or {}).get("stats",        {})

    if gen != "?":
        try:
            age_d = (datetime.now() - datetime.fromisoformat(gen)).days
            age_s = f"{age_d}d ago"
        except Exception:
            age_s = gen
    else:
        age_s = "?"

    print(f"  {_D}Cache: Journal/qualified_universe.json  |  "
          f"Built {age_s}  |  {days}d backtest{_RST}")
    print()

    if not qualified_set:
        print(f"  {_Y}  No qualified tickers yet.{_RST}")
        print(f"  {_D}  Run: python run.py --swing-backtest --days 180{_RST}\n")
        return

    col_w = [18, 6, 7, 7, 10, 12, 24]
    _table_header(["Ticker", "PF", "WR%", "AvgR", "MaxDD Rs", "Today", "Today status"], col_w)

    for tk in sorted(qualified_set):
        s      = stats.get(tk, {})
        pf     = s.get("pf",       0.0)
        wr     = s.get("win_rate", 0.0)
        avg_r  = s.get("avg_r",    0.0)
        max_dd = s.get("max_dd",   0.0)  # stored in INR, not %
        t_stat = today_status.get(tk, "NOT SCANNED")
        tc     = _STATUS_COLOR.get(t_stat, _D)

        if t_stat == "READY":
            note = "eligible — in entry zone"
        elif t_stat == "WATCH":
            note = "approaching — not yet"
        elif t_stat == "EXTENDED":
            note = "extended past entry"
        elif t_stat == "AVOID":
            note = "poor structure today"
        else:
            note = "not in today's scan"

        _row([tk, f"{pf:.2f}", f"{wr:.0f}%", f"{avg_r:+.2f}",
              f"Rs.{max_dd:,.0f}", t_stat, note],
             col_w,
             [_B, _G if pf >= 1.5 else _Y, "", "", _D, tc, _D])

    print()


def _print_decision_card(pick: dict, cap_info, stats: dict) -> None:
    ticker = str(pick.get("ticker",       "")).upper()
    entry  = float(pick.get("entry_price", 0) or 0)
    stop   = float(pick.get("stop_price",  0) or 0)
    target = float(pick.get("t1",          0) or 0)
    qty    = int(float(pick.get("quantity", 0) or 0))
    risk   = float(pick.get("max_loss_inr",0) or 0)
    rr     = float(pick.get("rr_t1",       0) or 0)
    grade_raw = str(pick.get("grade", ""))
    grade     = grade_raw if grade_raw.upper() not in ("", "NAN", "NONE") else "ungraded"
    score_raw = pick.get("score", None)
    try:
        score_f = float(score_raw)
        score_s = f"{score_f:.0f}/100" if score_f == score_f else "—"
    except (TypeError, ValueError):
        score_s = "—"
    s      = stats.get(ticker, {})
    pf_h   = s.get("pf",       0)
    wr_h   = s.get("win_rate", 0)
    W = _W
    print()
    print(f"  {_G}{'=' * (W + 4)}{_RST}")
    t = "TODAY'S DECISION"
    print(f"  {_G}{_B}{t.center(W + 4)}{_RST}")
    print(f"  {_G}{'=' * (W + 4)}{_RST}")
    print()
    print(f"  {_G}{_B}  PLACE  {ticker}{_RST}")
    print()

    def _ln(label: str, value: str, col: str = "") -> None:
        print(f"  {_D}{label:<18}{_RST}  {col}{value}{_RST if col else ''}")

    _ln("Entry",           f"Rs.{entry:,.2f}")
    _ln("Stop",            f"Rs.{stop:,.2f}   ({(entry-stop)/entry*100:+.2f}%)", _R)
    _ln("Target (T1)",     f"Rs.{target:,.2f}   (RR {rr:.2f}x)", _G)
    _ln("Quantity",        f"{qty} shares")
    _ln("Max risk",        f"Rs.{risk:,.0f}", _Y)
    _ln("Grade / Score",   f"{grade}  |  {score_s}")
    _ln("Backtest edge",   f"PF {pf_h:.2f}  |  WR {wr_h:.0f}%  |  180d validated", _G)
    if cap_info is not None:
        _ln("Capital avail", f"Rs.{cap_info.available:,.0f}  ({cap_info.source})")

    print()
    print(f"  {_C}{_B}  RUN:  python run.py --place {ticker}{_RST}")
    print()
    print(f"  {_G}{'=' * (W + 4)}{_RST}")
    print()
    print(f"  {_D}One trade. One decision. Place it and walk away.{_RST}\n")


_DECISION_TITLE = "TODAY'S DECISION"


def _print_no_trade_final(reason: str, action: str = "") -> None:
    W = _W
    print()
    print(f"  {_Y}{'=' * (W + 4)}{_RST}")
    print(f"  {_Y}{_B}{_DECISION_TITLE.center(W + 4)}{_RST}")
    print(f"  {_Y}{'=' * (W + 4)}{_RST}")
    print()
    print(f"  {_B}  NO TRADE TODAY{_RST}")
    print()
    print(f"  {_D}  Reason:{_RST}  {reason}")
    if action:
        print(f"  {_D}  Action:{_RST}  {_D}{action}{_RST}")
    print()
    print(f"  {_Y}{'=' * (W + 4)}{_RST}")
    print()
    print(f"  {_D}The scan above shows you everything. Do not go looking elsewhere.{_RST}\n")


def _print_doctor_blocked() -> None:
    W = _W
    print()
    print(f"  {_R}{'=' * (W + 4)}{_RST}")
    print(f"  {_R}{_B}{_DECISION_TITLE.center(W + 4)}{_RST}")
    print(f"  {_R}{'=' * (W + 4)}{_RST}")
    print()
    print(f"  {_R}{_B}  DO NOT TRADE{_RST}")
    print(f"  {_D}  Doctor reported critical failures above. Fix them first.{_RST}")
    print()
    print(f"  {_R}{'=' * (W + 4)}{_RST}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_morning(journal_dir: str = "journal", run_doctor_first: bool = True,
                refresh_scan: bool = False) -> int:
    """
    Returns:
      0 = decision printed (PLACE or NO-TRADE)
      1 = doctor hard-blocked
    """

    # 1. Doctor
    if run_doctor_first:
        from scanner.doctor import run_doctor
        print(f"\n  {_D}Running pre-flight check...{_RST}")
        rc = run_doctor(journal_dir=journal_dir, strict=False)
        if rc != 0:
            _print_doctor_blocked()
            return 1

    print()
    _divider(char="=", width=_W)
    print()

    # 2. Load universe + today's scan in parallel logic
    qualified_set, cache = _load_qualified_universe(journal_dir)
    cache_missing = not qualified_set and not cache
    cache_stale   = not qualified_set and bool(cache.get("_stale"))

    today_rows = _load_todays_scan(journal_dir=journal_dir)
    if not today_rows and refresh_scan:
        print(f"  {_D}No scan for today — running scanner...{_RST}\n")
        try:
            from scanner.daily_mode import run_daily_mode
            run_daily_mode(journal_dir=journal_dir)
        except Exception as e:
            print(f"  {_R}Scanner error: {e}{_RST}\n")
        today_rows = _load_todays_scan(journal_dir=journal_dir)

    # 3. Always show scan funnel (transparency first)
    today_status = _print_scan_funnel(today_rows, qualified_set)

    # 4. Always show qualified universe table
    _print_qualified_universe(qualified_set, cache, today_status)

    # 5. Hard gates
    if cache_missing:
        _print_no_trade_final(
            "qualified universe not built yet — no validated tickers to trade",
            "python run.py --swing-backtest --days 180   (run once monthly, ~15 min)",
        )
        return 0

    if cache_stale:
        _print_no_trade_final(
            f"qualified universe cache is {cache.get('_age_d','?')} days old (limit {_CACHE_MAX_AGE_DAYS}d)",
            "python run.py --swing-backtest --days 180   to refresh",
        )
        return 0

    if not today_rows:
        _print_no_trade_final(
            "no scan data for today",
            "python run.py --today   then re-run --morning",
        )
        return 0

    # 6. Funnel — qualified universe tickers pass even if grade is NaN (uncomputed)
    eligible = []
    for r in today_rows:
        tk        = str(r.get("ticker", "")).upper()
        status    = str(r.get("status", "")).upper()
        raw_grade = r.get("grade", None)
        grade     = str(raw_grade).upper() if raw_grade and str(raw_grade).upper() not in ("", "NAN", "NONE") else ""
        if status != "READY":
            continue
        if tk not in qualified_set:
            continue
        # Grade gate: A/A+ required for non-validated tickers.
        # For validated universe tickers, NaN grade = uncomputed (not bad) → allow.
        if grade and grade not in {"A+", "A"}:
            continue  # actively bad grade even for validated ticker → skip
        eligible.append(r)

    if not eligible:
        n_ready = sum(1 for r in today_rows if str(r.get("status","")).upper() == "READY")
        n_in_universe = sum(1 for r in today_rows
                            if str(r.get("status","")).upper() == "READY"
                            and str(r.get("ticker","")).upper() in qualified_set)
        if n_ready == 0:
            reason = "no READY setups in today's scan"
            action = "Market not offering entry zones. Nothing to do."
        elif n_in_universe == 0:
            reason = f"{n_ready} READY setup(s) but none in your validated universe"
            action = "python run.py --swing-backtest --days 180   to validate more tickers"
        else:
            # in universe but actively bad grade (B/AVOID)
            reason = (f"{n_in_universe} validated ticker(s) READY "
                      "but all have grade B or AVOID today — setup quality too low")
            action = "Wait for a better entry. The ticker needs a cleaner setup."
        _print_no_trade_final(reason, action)
        return 0

    # 7. Guards
    from scanner.guards import run_all_guards
    from config.config import CONFIG
    open_positions = _load_open_positions(journal_dir=journal_dir)

    try:
        from scanner.capital import get_effective_capital
        cap_info = get_effective_capital(CONFIG)
    except Exception:
        cap_info = None

    eligible.sort(key=lambda r: float(r.get("score", 0) or 0), reverse=True)

    _divider("GUARD CHECKS")
    print()
    chosen: Optional[dict] = None

    for cand in eligible:
        tk = str(cand.get("ticker","")).upper()
        plan = {
            "ticker":       tk,
            "entry_price":  float(cand.get("entry_price", 0) or 0),
            "max_loss_inr": float(cand.get("max_loss_inr", 0) or 0),
        }
        ctx = {
            "current_price":  plan["entry_price"],
            "today_open":     0,
            "first_seen":     str(cand.get("scan_date", "")),
            "open_positions": open_positions,
            "config":         CONFIG,
            "journal_dir":    journal_dir,
        }
        failures = run_all_guards(plan, ctx)
        if not failures:
            print(f"  {_G}PASS  {tk} — all guards cleared{_RST}")
            chosen = cand
            break
        for f in failures:
            print(f"  {_R}FAIL  {tk}  [{f.code}]  {f.reason}{_RST}")

    print()

    if chosen is None:
        _print_no_trade_final(
            "all candidates blocked by risk guards",
            "Review FAIL lines above. Common causes: portfolio heat, sector cap.",
        )
        return 0

    _print_decision_card(chosen, cap_info, (cache or {}).get("stats", {}))
    return 0

