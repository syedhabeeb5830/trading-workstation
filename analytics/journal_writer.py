"""
analytics/journal_writer.py — Atomic journal write primitives.

Pattern: write to sibling .tmp → fsync → os.replace (atomic rename).

On all supported platforms os.replace() is effectively atomic within the same
filesystem.  A crash at any point leaves one of two valid states:

  Before rename  → original file is intact; .tmp is discarded on next run
  After rename   → new file is live; nothing lost

The .tmp file is always in the same directory as the target so that the rename
is an intra-filesystem move (never a cross-device copy), which is the
requirement for atomicity on both POSIX and Windows (MoveFileEx +
MOVEFILE_REPLACE_EXISTING).
"""

from __future__ import annotations

import contextlib
import csv
import io
import os
from pathlib import Path
from typing import Sequence

import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# Core primitive
# ─────────────────────────────────────────────────────────────────────────────

def _write_atomic(path: Path, content: bytes) -> None:
    """
    Write *content* bytes to *path* atomically via a sibling temp file.
    Raises on I/O failure; never leaves a corrupt target.
    """
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


# ─────────────────────────────────────────────────────────────────────────────
# CSV helpers
# ─────────────────────────────────────────────────────────────────────────────

def atomic_append_rows(
    path: Path,
    rows: list[dict],
    fieldnames: Sequence[str],
) -> None:
    """
    Append *rows* to a CSV file at *path* atomically.

    Strategy:
      - Reads existing file bytes once into memory.
      - Builds the new row bytes via csv.DictWriter → StringIO.
      - Concatenates existing + new bytes in memory.
      - Writes the combined content atomically (tmp → fsync → rename).

    If *path* doesn't exist (or is empty) a header row is prepended
    automatically.  All subsequent appends skip the header.

    The existing file is never touched until the rename succeeds,
    so a crash at any point leaves the original file intact.
    """
    if not rows:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    has_existing = path.exists() and path.stat().st_size > 0

    # Build only the new portion (header + rows OR rows only)
    new_buf = io.StringIO()
    writer  = csv.DictWriter(
        new_buf, fieldnames=list(fieldnames),
        extrasaction="ignore", lineterminator="\r\n",
    )
    if not has_existing:
        writer.writeheader()
    for row in rows:
        writer.writerow(row)
    new_bytes = new_buf.getvalue().encode("utf-8")

    # Combine with existing content (byte-exact copy, no re-parsing)
    full_content = (path.read_bytes() if has_existing else b"") + new_bytes
    _write_atomic(path, full_content)


def atomic_csv_write(path: Path, df: pd.DataFrame) -> None:
    """
    Overwrite *path* with *df* serialised as CSV, atomically.

    Drop-in replacement for ``df.to_csv(path, index=False)`` that is safe
    against power failure during the write.
    """
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    _write_atomic(path, buf.getvalue().encode("utf-8"))


# ─────────────────────────────────────────────────────────────────────────────
# Startup cleanup
# ─────────────────────────────────────────────────────────────────────────────

def cleanup_orphaned_temp(path: Path) -> bool:
    """
    Remove a sibling *.tmp* file left by a previous crashed write.
    Should be called once at startup for each journal path.
    Returns *True* if a temp was found and removed.
    """
    tmp = path.with_suffix(".tmp")
    if tmp.exists():
        with contextlib.suppress(OSError):
            tmp.unlink()
        return True
    return False