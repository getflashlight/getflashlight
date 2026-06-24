"""Atomic GOLD publish.

A transform builds the new GOLD into a staging directory, then this swaps each
file into place with :func:`os.replace` — atomic per file on POSIX and Windows —
so a reader mid-query never sees a half-written ``<view>.parquet``. Cross-file
atomicity isn't needed: dashboards tolerate one view updating a beat before
another, and the next read reconciles.

Readers query GOLD via DuckDB's lazy ``read_parquet`` (see
:func:`auralake.lake.duck.register_gold`), so the file handle is held only for a
single query. On POSIX the swap always succeeds (the reader keeps the old inode);
on Windows ``MoveFileEx`` raises ``PermissionError`` if the destination happens to
be open at that instant, so :func:`_replace_with_retry` retries briefly to ride out
that millisecond-wide window.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from auralake.core.logging import get_logger

logger = get_logger(__name__)

# Windows-only: a reader's open handle can briefly block the rename. POSIX never
# retries (the first os.replace succeeds).
_REPLACE_RETRIES = 10
_REPLACE_BACKOFF_S = 0.05


def _replace_with_retry(src: Path, dst: Path) -> None:
    """``os.replace`` with a short retry on the transient Windows open-handle error."""
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRIES - 1:
                raise
            logger.warning("gold_publish_rename_retry", target=dst.name, attempt=attempt + 1)
            time.sleep(_REPLACE_BACKOFF_S)


def atomic_publish(staging: Path, target: Path) -> int:
    """Move every ``*.parquet`` from *staging* onto *target*, atomically per file.

    Returns the number of files published. The staging directory is removed
    afterwards. Files in *target* with no staged counterpart are left untouched
    (the catalog rebuilds every view each run, so they're always replaced).
    """
    target.mkdir(parents=True, exist_ok=True)
    published = 0
    for parquet in sorted(staging.glob("*.parquet")):
        _replace_with_retry(parquet, target / parquet.name)
        published += 1
        logger.info("gold_published", view=parquet.stem)
    shutil.rmtree(staging, ignore_errors=True)
    return published
