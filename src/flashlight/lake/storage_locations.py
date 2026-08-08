"""Storage-location writes: partition-replace the *current* snapshot per provider.

The purge+COPY kernel of :mod:`flashlight.lake.metrics` /
:mod:`flashlight.lake.driver_health`, with one deliberate difference: this takes **no**
``IngestWindow``.

Unity Catalog exposes only current state, so a pull always produces exactly one
snapshot — the month it ran in. Purging a *window* the way the sibling planes do would
delete five older snapshots on a six-month backfill while writing only one, silently
destroying the history that makes the map auditable. So the purge is scoped to the
snapshot month actually being written.

Older snapshots are kept on purpose: ``gold.backing_storage_month`` reads only the
newest one, but the history is the audit trail for "when did this bucket become
Databricks-backed?".
"""

from __future__ import annotations

import shutil

from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.lake import duck, paths
from flashlight.lake.storage_location_schema import (
    StorageLocationRecord,
    build_table,
    snapshot_month_of,
)

logger = get_logger(__name__)


def _copy_options() -> str:
    settings = get_settings()
    opts = [
        "FORMAT parquet",
        "PARTITION_BY (provider_name, snapshot_month)",
        f"COMPRESSION '{settings.parquet_compression}'",
        "APPEND",
    ]
    if settings.parquet_compression == "zstd":
        opts.insert(3, f"COMPRESSION_LEVEL {settings.parquet_compression_level}")
    return ", ".join(opts)


def _purge_snapshots(partitions: set[tuple[str, str]]) -> None:
    """Remove exactly the ``(provider, snapshot_month)`` dirs about to be rewritten."""
    for provider, month in partitions:
        partition = (
            paths.storage_locations_dir()
            / f"provider_name={provider}"
            / f"snapshot_month={month}"
        )
        if partition.exists():
            shutil.rmtree(partition)
            logger.info(
                "storage_locations_partition_purged", provider=provider, snapshot_month=month
            )


def write_storage_locations(records: list[StorageLocationRecord]) -> int:
    """Replace each ``(provider, snapshot_month)`` present in *records* with *records*.

    Returns rows written. An empty list is a no-op rather than a purge: unlike a cost
    window — where "the source no longer reports this month" is real information and
    self-purging is the point — a metadata pull that returns nothing means the API call
    failed or the token lacks permission (both best-effort, see
    ``Connector.fetch_storage_locations``). Deleting a good map because a later pull
    couldn't see anything would turn a transient permission problem into permanent data
    loss, and would make the dashboard say "no Databricks storage" — the one thing the
    backing-storage tab must never imply from absence.
    """
    if not records:
        logger.info("storage_locations_empty_pull_kept_existing_map")
        return 0

    _purge_snapshots({(str(r.provider_name), snapshot_month_of(r)) for r in records})

    table = build_table(records)
    paths.storage_locations_dir().mkdir(parents=True, exist_ok=True)
    con = duck.connect()
    try:
        con.register("_rows", table)
        con.execute(f"COPY _rows TO '{paths.storage_locations_dir()}' ({_copy_options()})")  # noqa: S608
    finally:
        con.close()
    logger.info("storage_locations_written", rows=len(records))
    return len(records)
