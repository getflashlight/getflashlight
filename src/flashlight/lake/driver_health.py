"""Typed Bronze driver-health writes, partition-replaced by provider and month.

``DriverHealthRecord`` does not fit the FOCUS-cost schema, so it is persisted in its
own relation under ``bronze/driver_health``. Source rows are already unique per
(driver, application, user, month), so no within-batch dedupe is needed.
"""

from __future__ import annotations

import shutil
from datetime import date

from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.ingest.base import IngestWindow
from flashlight.lake import duck, paths
from flashlight.lake.driver_health_schema import DriverHealthRecord, build_table

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
        provider_dir = paths.bronze_driver_health_dir() / f"provider_name={provider}"
        for month in months:
            partition = provider_dir / f"charge_month={month}"
            if partition.exists():
                shutil.rmtree(partition)
                logger.info("driver_health_partition_purged", provider=provider, charge_month=month)


def write_driver_health(window: IngestWindow, records: list[DriverHealthRecord]) -> int:
    """Partition-replace ``[window]`` with ``records``. Returns rows written."""
    providers = {str(r.provider_name) for r in records}
    _purge_window(providers, window)
    if not records:
        logger.info("driver_health_window_empty")
        return 0

    table = build_table(records)
    paths.bronze_driver_health_dir().mkdir(parents=True, exist_ok=True)
    con = duck.connect()
    try:
        con.register("_rows", table)
        con.execute(
            f"COPY _rows TO '{paths.bronze_driver_health_dir()}' ({_copy_options()})"
        )  # noqa: S608
    finally:
        con.close()
    logger.info("driver_health_window_written", rows=len(records))
    return len(records)
