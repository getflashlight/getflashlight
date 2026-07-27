"""Atomic GOLD publish.

A transform builds the new GOLD into a staging tree (``<group>/<view>.parquet``,
one group per provider plus ``shared``), then this swaps each file into place with
:func:`os.replace` — atomic per file on POSIX and Windows — so a reader mid-query
never sees a half-written ``<view>.parquet``. Cross-file atomicity isn't needed:
dashboards tolerate one view updating a beat before another, and the next read
reconciles. Group dirs present in ``gold/`` but absent from staging (a provider that
dropped out of the data) are pruned so the published set always matches the data.

Readers query GOLD via DuckDB's lazy ``read_parquet`` (see
:func:`flashlight.lake.duck.register_gold`), so the file handle is held only for a
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

from flashlight.core.logging import get_logger

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
    """Move every ``<group>/*.parquet`` from *staging* onto *target*, atomically per file.

    Returns the number of files published. Each file is swapped into
    ``target/<group>/<view>.parquet`` (the group dir is created on first publish).
    Group dirs in *target* with no staged counterpart are pruned, so a provider that
    dropped out of the data loses its GOLD. The staging tree is removed afterwards.
    """
    target.mkdir(parents=True, exist_ok=True)
    staged_groups: set[str] = set()
    published = 0
    for parquet in sorted(staging.glob("*/*.parquet")):
        rel = parquet.relative_to(staging)  # e.g. aws/monthly_bill.parquet
        staged_groups.add(rel.parts[0])
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        _replace_with_retry(parquet, dest)
        published += 1
        logger.info("gold_published", group=rel.parts[0], view=parquet.stem)

    # Prune group dirs that are no longer produced (provider gone from the data).
    for group_dir in target.iterdir():
        if group_dir.is_dir() and group_dir.name not in staged_groups:
            shutil.rmtree(group_dir, ignore_errors=True)
            logger.info("gold_group_pruned", group=group_dir.name)

    shutil.rmtree(staging, ignore_errors=True)
    return published
