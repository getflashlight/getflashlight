"""Compute-instance writes: partition-replace per (provider, charge-month window).

Copies :mod:`flashlight.lake.driver_health`'s purge+COPY kernel verbatim for the
sibling ``ComputeInstanceRecord`` dataset — same shape, different Parquet root
(:func:`~flashlight.lake.paths.compute_instances_dir`) so it doesn't collide with the
efficiency plane's recursive glob (see that function's docstring), and the same
purge-scoped-to-the-providers-actually-returned behavior: a connector that returns zero
rows purges nothing (there's no provider name to scope the purge to), same as
``driver_health`` and unlike BRONZE's ``write_window`` (which purges its one known
connector regardless of row count). Unlike ``storage_locations`` (a present-tense UC
snapshot with a deliberate no-op-on-empty-pull rule), a *non-empty* result here really is
a genuine charge-period fact and gets a real partition-replace across the whole window.
"""

from __future__ import annotations

import shutil
from datetime import date

from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.ingest.base import IngestWindow
from flashlight.lake import duck, paths
from flashlight.lake.compute_instance_schema import ComputeInstanceRecord, build_table

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
        provider_dir = paths.compute_instances_dir() / f"provider_name={provider}"
        for month in months:
            partition = provider_dir / f"charge_month={month}"
            if partition.exists():
                shutil.rmtree(partition)
                logger.info(
                    "compute_instances_partition_purged", provider=provider, charge_month=month
                )


def write_compute_instances(window: IngestWindow, records: list[ComputeInstanceRecord]) -> int:
    """Partition-replace ``[window]`` with ``records``. Returns rows written."""
    providers = {str(r.provider_name) for r in records}
    _purge_window(providers, window)
    if not records:
        logger.info("compute_instances_window_empty")
        return 0

    table = build_table(records)
    paths.compute_instances_dir().mkdir(parents=True, exist_ok=True)
    con = duck.connect()
    try:
        con.register("_rows", table)
        con.execute(f"COPY _rows TO '{paths.compute_instances_dir()}' ({_copy_options()})")  # noqa: S608
    finally:
        con.close()
    logger.info("compute_instances_window_written", rows=len(records))
    return len(records)
