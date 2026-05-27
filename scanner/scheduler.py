"""
scanner/scheduler.py — Windows Task Scheduler installer
=========================================================
Registers a scheduled task that runs `python run.py --positions --quiet`
every 15 minutes on weekdays from 09:15 to 15:30 IST.

WHY: catches GTT fills + places protective stops automatically, without
needing to babysit a terminal. Pairs with winotify toasts so you get
desktop alerts the moment a position opens.

Usage:
    python run.py --install-scheduler        # one-time setup
    python run.py --uninstall-scheduler      # remove the task

The task is stored under \\SwingTrader\\AutoPositions in Task Scheduler.
You can edit / disable it via taskschd.msc.
"""
from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path

TASK_NAME         = r"\SwingTrader\AutoPositions"
TASK_NAME_MORNING = r"\SwingTrader\MorningBriefing"


def _venv_python() -> str:
    """Find the project's venv python.exe."""
    here = Path(__file__).resolve().parent.parent
    p = here / ".venv" / "Scripts" / "python.exe"
    return str(p) if p.exists() else sys.executable


def _project_dir() -> str:
    return str(Path(__file__).resolve().parent.parent)


def install_scheduler() -> None:
    """Create the scheduled task. Idempotent — replaces existing task."""
    python = _venv_python()
    cwd    = _project_dir()
    # Wrapper batch ensures correct working directory + UTF-8 encoding
    wrapper = Path(cwd) / ".scheduled_positions.bat"
    wrapper.write_text(
        "@echo off\r\n"
        f'cd /d "{cwd}"\r\n'
        "set PYTHONIOENCODING=utf-8\r\n"
        f'"{python}" run.py --positions --quiet >> journal\\scheduler.log 2>&1\r\n',
        encoding="ascii",
    )

    # schtasks.exe: run every 15 min, daily, weekdays only
    cmd = [
        "schtasks", "/Create", "/F",
        "/TN", TASK_NAME,
        "/TR", str(wrapper),
        "/SC", "MINUTE",
        "/MO", "15",
        "/ST", "09:15",
        "/ET", "15:30",
        "/DURATION", "06:30",
        "/K",
        "/RL", "LIMITED",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        print("  ✗ schtasks.exe not found — are you on Windows?")
        return
    if result.returncode == 0:
        print(f"\n  ✓ Scheduled task installed: {TASK_NAME}")
        print(f"     Wrapper:    {wrapper}")
        print(f"     Schedule:   Every 15 min, 09:15–15:30 IST (Mon–Fri)")
        print(f"     Log:        journal/scheduler.log")
    else:
        print(f"\n  ✗ schtasks failed (code {result.returncode}):")
        print(f"    {result.stderr.strip() or result.stdout.strip()}")
        print(f"\n  Try running as Administrator, or install manually:")
        print(f"    schtasks /Create /TN {TASK_NAME} /TR \"{wrapper}\" ...")

    # ── Morning briefing task: run --today once at 09:00 Mon–Fri ─────────────
    wrapper_morning = Path(cwd) / ".scheduled_today.bat"
    wrapper_morning.write_text(
        "@echo off\r\n"
        f'cd /d "{cwd}"\r\n'
        "set PYTHONIOENCODING=utf-8\r\n"
        f'"{python}" run.py --today >> journal\\morning.log 2>&1\r\n',
        encoding="ascii",
    )
    cmd_morning = [
        "schtasks", "/Create", "/F",
        "/TN", TASK_NAME_MORNING,
        "/TR", str(wrapper_morning),
        "/SC", "WEEKLY",
        "/D", "MON,TUE,WED,THU,FRI",
        "/MO", "1",
        "/ST", "09:00",
        "/RL", "LIMITED",
    ]
    try:
        result_m = subprocess.run(cmd_morning, capture_output=True, text=True)
    except FileNotFoundError:
        print("  ✗ schtasks.exe not found for morning task.")
        return
    if result_m.returncode == 0:
        print(f"\n  ✓ Morning briefing task installed: {TASK_NAME_MORNING}")
        print(f"     Wrapper:    {wrapper_morning}")
        print(f"     Schedule:   09:00 IST, Mon–Fri  (runs --today automatically)")
        print(f"     Log:        journal/morning.log")
    else:
        print(f"\n  ✗ Morning task failed (code {result_m.returncode}):")
        print(f"    {result_m.stderr.strip() or result_m.stdout.strip()}")

    print(f"\n     View / edit:   taskschd.msc  →  Task Scheduler Library  →  SwingTrader")
    print(f"     Remove:        python run.py --uninstall-scheduler\n")


def uninstall_scheduler() -> None:
    """Remove both scheduled tasks (positions + morning briefing)."""
    cwd = _project_dir()
    for task, bat in [
        (TASK_NAME,         ".scheduled_positions.bat"),
        (TASK_NAME_MORNING, ".scheduled_today.bat"),
    ]:
        cmd = ["schtasks", "/Delete", "/F", "/TN", task]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            print("  ✗ schtasks.exe not found.")
            return
        if result.returncode == 0:
            print(f"\n  ✓ Scheduled task removed: {task}")
            bat_path = Path(cwd) / bat
            if bat_path.exists():
                bat_path.unlink()
        else:
            print(f"  Task not installed (or already removed): {task}")
    print()
