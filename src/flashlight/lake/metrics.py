"""Metrics-plane writes: partition-replace per (provider, charge-month window).

The waste-plane sibling of :mod:`flashlight.lake.bronze`. Same kernel — purge the
window's partition dirs, then a single DuckDB ``COPY`` with ``PARTITION_BY`` — but
keyed on ``provider_name=<p>/charge_month=<YYYY-MM>/`` and over the already-aggregated
:class:`~flashlight.efficiency.model.EfficiencyRecord` rows. Re-running a window is
idempotent and self-purging, exactly as for BRONZE.

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
        "PARTITION_BY (provider_name, charge_month)",
        f"COMPRESSION '{settings.parquet_compression}'",
        "APPEND",
    ]
    if settings.parquet_compression == "zstd":
        opts.insert(3, f"COMPRESSION_LEVEL {settings.parquet_compression_level}")
    return ", ".join(opts)


def _purge_window(providers: set[str], window: IngestWindow) -> None:
    """Remove each provider's partition dirs across the window — the delete-window step."""
    months = _window_months(window.start, window.end)
    for provider in providers:
        provider_dir = paths.metrics_dir() / f"provider_name={provider}"
        for month in months:
            partition = provider_dir / f"charge_month={month}"
            if partition.exists():
                shutil.rmtree(partition)
                logger.info("metrics_partition_purged", provider=provider, charge_month=month)


def write_efficiency(window: IngestWindow, records: list[EfficiencyRecord]) -> int:
    """Partition-replace ``[window]`` with ``records``. Returns rows written.

    Purges each present provider's window partitions first (authoritative), then writes
    the fresh, source-aggregated pull as zstd Parquet partitioned by provider + month.
    """
    providers = {str(r.provider_name) for r in records}
    _purge_window(providers, window)
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
