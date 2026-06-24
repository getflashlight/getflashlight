"""Atomic GOLD publish.

A transform builds the new GOLD into a staging directory, then this swaps each
file into place with :func:`os.replace` — atomic per file on POSIX and Windows —
so a reader mid-query never sees a half-written ``<view>.parquet``. Cross-file
atomicity isn't needed: dashboards tolerate one view updating a beat before
another, and the next read reconciles.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from auralake.core.logging import get_logger

logger = get_logger(__name__)


def atomic_publish(staging: Path, target: Path) -> int:
    """Move every ``*.parquet`` from *staging* onto *target*, atomically per file.

    Returns the number of files published. The staging directory is removed
    afterwards. Files in *target* with no staged counterpart are left untouched
    (the catalog rebuilds every view each run, so they're always replaced).
    """
    target.mkdir(parents=True, exist_ok=True)
    published = 0
    for parquet in sorted(staging.glob("*.parquet")):
        os.replace(parquet, target / parquet.name)
        published += 1
        logger.info("gold_published", view=parquet.stem)
    shutil.rmtree(staging, ignore_errors=True)
    return published
