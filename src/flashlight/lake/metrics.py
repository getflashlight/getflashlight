"""Metrics-plane writes: partition-replace per (provider, connector, charge-month).

The waste-plane sibling of :mod:`flashlight.lake.bronze`. Same kernel — purge the
window's partition dirs, then a single DuckDB ``COPY`` with ``PARTITION_BY`` — but
keyed on ``provider_name=<p>/x_source_connector=<source>/charge_month=<YYYY-MM>/``
and over the already-aggregated :class:`~flashlight.efficiency.model.EfficiencyRecord`
rows. Re-running a window is idempotent and self-purging, exactly as for BRONZE. In
particular, a refresh of one Redshift cluster cannot purge a sibling cluster's AWS
telemetry for the same billing month.

Source-aggregated rows are unique per (entity × month), so there is no within-batch
dedupe step (unlike BRONZE's ``dedupe``).
"""

from __future__ import annotations

import shutil
from datetime import date

from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.efficiency.model import EfficiencyRecord
from flashlight.ingest.base import IngestWindow
from flashlight.lake import duck, paths
from flashlight.lake.metrics_schema import build_table

logger = get_logger(__name__)


def _window_months(start: date, end: date) -> list[str]:
    """Every ``YYYY-MM`` partition value touched by the inclusive window [start, end]."""
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return months


def _copy_options() -> str:
    settings = get_settings()
    opts = [
        "FORMAT parquet",
        "PARTITION_BY (provider_name, x_source_connector, charge_month)",
        f"COMPRESSION '{settings.parquet_compression}'",
        "APPEND",
    ]
    if settings.parquet_compression == "zstd":
        opts.insert(3, f"COMPRESSION_LEVEL {settings.parquet_compression_level}")
    return ", ".join(opts)


def _purge_window(partitions: set[tuple[str, str]], window: IngestWindow) -> None:
    """Remove only each provider/connector's month partitions in the window."""
    months = _window_months(window.start, window.end)
    for provider, connector in partitions:
        connector_dir = (
            paths.metrics_dir()
            / f"provider_name={provider}"
            / f"x_source_connector={connector}"
        )
        for month in months:
            partition = connector_dir / f"charge_month={month}"
            if partition.exists():
                shutil.rmtree(partition)
                logger.info(
                    "metrics_partition_purged",
                    provider=provider,
                    connector=connector,
                    charge_month=month,
                )


def _migrate_legacy_provider_month_partitions() -> None:
    """Upgrade old provider/month-only files before writing the new connector grain.

    Without this one-time split, old files would be read beside new connector-scoped
    files and a later refresh would duplicate their rows.  The old files already carry
    ``x_source_connector`` as a normal Parquet column, so the migration can safely
    repartition them without guessing a cluster identity.
    """
    root = paths.metrics_dir()
    legacy_dirs = [
        month_dir
        for provider_dir in root.glob("provider_name=*")
        if provider_dir.is_dir()
        for month_dir in provider_dir.glob("charge_month=*")
        if month_dir.is_dir() and list(month_dir.glob("*.parquet"))
    ]
    if not legacy_dirs:
        return

    files = [file for partition in legacy_dirs for file in partition.glob("*.parquet")]
    literal_files = ", ".join(f"'{str(file).replace("'", "''")}'" for file in files)
    source = f"read_parquet([{literal_files}], hive_partitioning=true, union_by_name=true)"
    con = duck.connect()
    try:
        con.execute(f"COPY (SELECT * FROM {source}) TO '{root}' ({_copy_options()})")  # noqa: S608
    finally:
        con.close()
    for partition in legacy_dirs:
        shutil.rmtree(partition)
    logger.info("metrics_legacy_partitions_migrated", partitions=len(legacy_dirs), files=len(files))


def write_efficiency(window: IngestWindow, records: list[EfficiencyRecord]) -> int:
    """Partition-replace ``[window]`` with ``records``. Returns rows written.

    Purges each present provider/connector's window partitions first (authoritative), then
    writes the fresh, source-aggregated pull as zstd Parquet partitioned by provider,
    connector, and month.
    """
    _migrate_legacy_provider_month_partitions()
    partitions = {(str(r.provider_name), r.x_source_connector) for r in records}
    _purge_window(partitions, window)
    if not records:
        logger.info("metrics_window_empty")
        return 0

    table = build_table(records)
    paths.metrics_dir().mkdir(parents=True, exist_ok=True)
    con = duck.connect()
    try:
        con.register("_rows", table)
        con.execute(f"COPY _rows TO '{paths.metrics_dir()}' ({_copy_options()})")  # noqa: S608
    finally:
        con.close()
    logger.info("metrics_window_written", rows=len(records))
    return len(records)
