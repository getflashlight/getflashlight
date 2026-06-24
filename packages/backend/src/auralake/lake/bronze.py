"""BRONZE writes: partition-replace per (connector, charge-period window).

Cloud billing is restated data — Databricks appends corrections, AWS CUR
re-delivers whole months. So ingest is authoritative for the window it pulls:
:func:`write_window` purges the connector's existing partition directories across
the window's months, then writes the fresh pull. Re-running the same window is
therefore idempotent AND self-purging — a month the source no longer reports has
its partition removed and not rewritten, so orphaned rows can't survive.

Partition layout is ``x_source_connector=<conn>/charge_month=<YYYY-MM>/``, so the
purge is a directory ``rmtree`` and the write is a single DuckDB ``COPY`` with
``PARTITION_BY``. There is no transaction to coordinate: the only reader of BRONZE
is the transform that runs immediately after, in the same process.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, date, datetime

from auralake.core.logging import get_logger
from auralake.core.settings import get_settings
from auralake.focus.model import FocusRecord
from auralake.ingest.base import IngestWindow
from auralake.lake import duck, paths
from auralake.lake.schema import build_table

logger = get_logger(__name__)


def new_run_id() -> str:
    """A unique id stamped on every row of one ingest run (links to the run log)."""
    return uuid.uuid4().hex


def collapse_duplicates(records: list[FocusRecord]) -> list[FocusRecord]:
    """Collapse records sharing a ``dedupe_key`` (last wins) — a within-batch guard.

    A single pull must not contain the same physical source row twice. The key
    includes ``x_record_id``/``x_record_type``, so Databricks ORIGINAL/RETRACTION/
    RESTATEMENT do NOT collapse — they net later via ``SUM`` in SILVER/GOLD. A real
    collision here means the identical row appeared twice in one pull, so last-wins
    loses nothing.
    """
    by_key: dict[str, FocusRecord] = {}
    for record in records:
        by_key[record.dedupe_key()] = record
    if len(by_key) != len(records):
        logger.info(
            "collapsed_duplicate_rows",
            input_rows=len(records),
            output_rows=len(by_key),
            dropped=len(records) - len(by_key),
        )
    return list(by_key.values())


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


def _purge_window(connector: str, window: IngestWindow) -> None:
    """Remove the connector's partition dirs across the window — the delete-window step."""
    connector_dir = paths.bronze_dir() / f"x_source_connector={connector}"
    for month in _window_months(window.start, window.end):
        partition = connector_dir / f"charge_month={month}"
        if partition.exists():
            shutil.rmtree(partition)
            logger.info("bronze_partition_purged", connector=connector, charge_month=month)


def copy_options() -> str:
    """DuckDB ``COPY`` options for a partitioned, zstd BRONZE write.

    Shared by the row-based connector path (:func:`write_window`) and the
    vectorized file path (:mod:`auralake.lake.seed`), so the partition layout and
    compression settings can never drift between them.
    """
    settings = get_settings()
    opts = [
        "FORMAT parquet",
        "PARTITION_BY (x_source_connector, charge_month)",
        f"COMPRESSION '{settings.parquet_compression}'",
        "APPEND",
    ]
    if settings.parquet_compression == "zstd":
        opts.insert(3, f"COMPRESSION_LEVEL {settings.parquet_compression_level}")
    return ", ".join(opts)


def purge_connector(connector: str) -> None:
    """Remove all of a connector's BRONZE partitions (full replace for that source)."""
    connector_dir = paths.bronze_dir() / f"x_source_connector={connector}"
    if connector_dir.exists():
        shutil.rmtree(connector_dir)
        logger.info("bronze_connector_purged", connector=connector)


def write_window(
    connector: str,
    window: IngestWindow,
    records: list[FocusRecord],
    *,
    ingest_run_id: str,
) -> int:
    """Partition-replace ``[window]`` for ``connector`` with ``records``. Returns rows.

    Purges the window's partitions first (authoritative), then writes the fresh,
    deduplicated pull as zstd Parquet partitioned by connector + charge month.
    """
    deduped = collapse_duplicates(records)
    _purge_window(connector, window)
    if not deduped:
        logger.info("bronze_window_empty", connector=connector)
        return 0

    table = build_table(deduped, ingest_run_id=ingest_run_id, ingested_at=datetime.now(UTC))
    paths.bronze_dir().mkdir(parents=True, exist_ok=True)
    con = duck.connect()
    try:
        con.register("_rows", table)
        con.execute(
            f"COPY _rows TO '{paths.bronze_dir()}' ({copy_options()})"  # noqa: S608
        )
    finally:
        con.close()
    logger.info("bronze_window_written", connector=connector, rows=len(deduped))
    return len(deduped)
