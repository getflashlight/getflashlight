"""``flashlight cleanup`` — wipe all lake data (Parquet) under ``FLASHLIGHT_HOME``.

The nuclear counterpart to ``sample --clean`` (which is scoped to the generated
demo's partitions): this removes *everything* the writers produce —
every BRONZE partition, every published GOLD view, any half-built staging dir,
and the ingest run log — leaving only ``config/`` so the install stays usable.
Idempotent: a no-op on a never-seeded home. Config (``connections.yml``) and any
downloaded sample CSVs under ``data/`` are left untouched.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

from flashlight.core.logging import get_logger
from flashlight.lake import bronze, paths

logger = get_logger(__name__)


def _data_dirs() -> Iterator[tuple[str, Path]]:
    """The (label, path) of every dir that holds generated lake data — not config."""
    yield "bronze", paths.bronze_dir()
    # Pre-Bronze driver health is read during the migration but no longer written.
    yield "legacy_driver_health", paths.legacy_driver_health_dir()
    yield "gold", paths.gold_dir()
    yield "gold.staging", paths.gold_staging_dir()
    yield "runs", paths.runs_dir()
    yield "assistant_turns", paths.assistant_turns_dir()


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


def connector_targets(connector: str) -> list[Path]:
    """The paths a scoped cleanup for *connector* would remove (dry-run / confirmation).

    Scoped to BRONZE — a connector's cost pull — the same grain
    :func:`~flashlight.lake.bronze.purge_connector` already operates on (this is the
    ``sample --clean`` pattern, generalized to any connector name). GOLD isn't listed:
    it's rebuilt, not deleted, by the caller re-running ``transform``/``ingest``
    afterward. Efficiency telemetry (``metrics/``) isn't connector-partitioned on disk
    (only by ``provider_name``/``charge_month``), so it's out of scope here.
    """
    targets = []
    connector_dir = paths.bronze_dir() / f"x_source_connector={connector}"
    if connector_dir.exists() and any(connector_dir.iterdir()):
        targets.append(connector_dir)
    if paths.runs_dir().exists():
        targets.extend(sorted(paths.runs_dir().glob(f"*-{connector}.parquet")))
    return targets


def purge_connector(connector: str) -> int:
    """Remove one connector's BRONZE partitions + run-log entries. Returns paths removed.

    Leaves every other connector's data — and GOLD — untouched; call
    ``build_gold()`` afterward to refresh GOLD from what remains in BRONZE.
    """
    removed = 0
    if (paths.bronze_dir() / f"x_source_connector={connector}").exists():
        bronze.purge_connector(connector)
        removed += 1
    if paths.runs_dir().exists():
        for run in paths.runs_dir().glob(f"*-{connector}.parquet"):
            run.unlink()
            removed += 1
    return removed
