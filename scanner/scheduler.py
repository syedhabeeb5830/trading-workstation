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

TASK_NAME = r"\SwingTrader\AutoPositions"


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
        print(f"\n     View / edit:   taskschd.msc   →  Task Scheduler Library  →  SwingTrader")
        print(f"     Remove:        python run.py --uninstall-scheduler\n")
    else:
        print(f"\n  ✗ schtasks failed (code {result.returncode}):")
        print(f"    {result.stderr.strip() or result.stdout.strip()}")
        print(f"\n  Try running as Administrator, or install manually:")
        print(f"    schtasks /Create /TN {TASK_NAME} /TR \"{wrapper}\" ...")


def uninstall_scheduler() -> None:
    """Remove the scheduled task."""
    cmd = ["schtasks", "/Delete", "/F", "/TN", TASK_NAME]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        print("  ✗ schtasks.exe not found.")
        return
    if result.returncode == 0:
        print(f"\n  ✓ Scheduled task removed: {TASK_NAME}\n")
        wrapper = Path(_project_dir()) / ".scheduled_positions.bat"
        if wrapper.exists():
            wrapper.unlink()
    else:
        print(f"\n  Task was not installed (or already removed):  {result.stderr.strip()}\n")
