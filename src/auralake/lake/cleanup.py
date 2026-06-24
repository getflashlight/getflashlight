"""``auralake cleanup`` — wipe all lake data (Parquet) under ``AURALAKE_HOME``.

The nuclear counterpart to ``sample --clean`` (which is scoped to the isolated
``focus_sample`` connector): this removes *everything* the writers produce —
every BRONZE partition, every published GOLD view, any half-built staging dir,
and the ingest run log — leaving only ``config/`` so the install stays usable.
Idempotent: a no-op on a never-seeded home. Config (``connections.yml``) and any
downloaded sample CSVs under ``data/`` are left untouched.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

from auralake.core.logging import get_logger
from auralake.lake import paths

logger = get_logger(__name__)


def _data_dirs() -> Iterator[tuple[str, Path]]:
    """The (label, path) of every dir that holds generated lake data — not config."""
    yield "bronze", paths.bronze_dir()
    yield "gold", paths.gold_dir()
    yield "gold.staging", paths.gold_staging_dir()
    yield "runs", paths.runs_dir()


def cleanup_targets() -> list[Path]:
    """The non-empty data dirs a cleanup would remove (for dry-run / confirmation).

    An empty dir (e.g. the skeleton :func:`purge_all` recreates) is *not* a target,
    so a second cleanup correctly reports nothing to clean.
    """
    return [path for _, path in _data_dirs() if path.exists() and any(path.iterdir())]


def purge_all() -> list[str]:
    """Remove all generated lake data, preserving config. Returns the labels removed."""
    removed: list[str] = []
    for label, path in _data_dirs():
        if path.exists():
            shutil.rmtree(path)
            logger.info("lake_data_purged", target=label, path=str(path))
            removed.append(label)
    # Recreate the empty skeleton so readers/writers find their dirs.
    paths.ensure_layout()
    return removed
