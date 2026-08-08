"""BRONZE writes: partition-replace per (connector, charge-period window).

Cloud billing is restated data — Databricks appends corrections, AWS CUR
re-delivers whole months. So ingest is authoritative for the window it pulls:
:func:`write_window` purges the connector's existing partition directories across
the window's months, then writes the fresh pull. Re-running the same window is
therefore idempotent AND self-purging — a month the source no longer reports has
its partition removed and not rewritten, so orphaned rows can't survive.

Partition layout is ``x_source_connector=<conn>/charge_month=<YYYY-MM>/``, so the
purge is a directory ``rmtree`` and the write is one or more DuckDB ``COPY ...
APPEND`` calls with ``PARTITION_BY``. There is no transaction to coordinate: the
only reader of BRONZE is the transform that runs immediately after, in the same
process.

``write_window`` streams its input in chunks (:data:`CHUNK_ROWS` records at a
time) rather than materializing the whole pull as one Arrow table, so a
million-row month doesn't hold every :class:`FocusRecord` in Python memory at
once. Because the purge happens before any chunk is written, a failure partway
through a stream would otherwise leave a half-written window; :func:`write_window`
re-purges on error so a failed pull always ends with an empty (not partial)
window, never a stale-partial one.
"""

from __future__ import annotations

import itertools
import shutil
import uuid
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime

import duckdb

from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.focus.model import FocusRecord
from flashlight.focus.sql_mapping import assert_single_currency
from flashlight.ingest.base import IngestWindow
from flashlight.lake import duck, paths
from flashlight.lake.schema import build_table

logger = get_logger(__name__)

# Records per COPY call — bounds peak memory to one chunk of FocusRecord + its
# Arrow table, regardless of how many rows the connector streams overall.
CHUNK_ROWS = 100_000


def new_run_id() -> str:
    """A unique id stamped on every row of one ingest run (links to the run log)."""
    return uuid.uuid4().hex


def dedupe(records: Iterable[FocusRecord]) -> Iterator[FocusRecord]:
    """Drop records sharing a ``dedupe_key`` (first wins) — a within-batch guard.

    A single pull must not contain the same physical source row twice. The key
    includes ``x_record_id``/``x_record_type``, so Databricks ORIGINAL/RETRACTION/
    RESTATEMENT do NOT collapse — they net later via ``SUM`` in SILVER/GOLD. A real
    collision here means the identical row appeared twice in one pull, so which
    copy wins is immaterial — first-wins lets this stream instead of buffering the
    whole pull to find the last one.

    # ponytail: an in-memory set of 32-byte digests, one per distinct row in the
    # window (~tens of MB even at 100M rows). Spill to a DuckDB table if a single
    # window's distinct-row count ever gets large enough for that to matter.
    """
    seen: set[bytes] = set()
    total = dropped = 0
    for record in records:
        total += 1
        digest = bytes.fromhex(record.dedupe_key())
        if digest in seen:
            dropped += 1
            continue
        seen.add(digest)
        yield record
    if dropped:
        logger.info(
            "collapsed_duplicate_rows",
            input_rows=total,
            output_rows=total - dropped,
            dropped=dropped,
        )


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
    vectorized file path (:mod:`flashlight.lake.seed`), so the partition layout and
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
    records: Iterable[FocusRecord],
    *,
    ingest_run_id: str,
) -> int:
    """Partition-replace ``[window]`` for ``connector`` with ``records``. Returns rows.

    Purges the window's partitions first (authoritative), then streams the
    deduplicated pull to zstd Parquet — partitioned by connector + charge month —
    in :data:`CHUNK_ROWS`-sized chunks, so ``records`` never needs to be fully
    materialized in memory. If the stream raises partway through, the window is
    re-purged before the error propagates, so a failed pull never leaves a
    partial partition behind.
    """
    _purge_window(connector, window)
    ingested_at = datetime.now(UTC)
    paths.bronze_dir().mkdir(parents=True, exist_ok=True)

    total = 0
    con = duck.connect()
    try:
        deduped = dedupe(records)
        while chunk := list(itertools.islice(deduped, CHUNK_ROWS)):
            table = build_table(chunk, ingest_run_id=ingest_run_id, ingested_at=ingested_at)
            con.register("_rows", table)
            con.execute(
                f"COPY _rows TO '{paths.bronze_dir()}' ({copy_options()})"  # noqa: S608
            )
            total += len(chunk)
    except Exception:
        _purge_window(connector, window)
        raise
    finally:
        con.close()

    if not total:
        logger.info("bronze_window_empty", connector=connector)
        return 0
    logger.info("bronze_window_written", connector=connector, rows=total)
    return total


def write_window_sql(
    connector: str,
    window: IngestWindow,
    con: duckdb.DuckDBPyConnection,
    mapped_sql: str,
    *,
    base_currency: str,
) -> int:
    """The vectorized sibling of :func:`write_window`: partition-replace ``[window]``
    for ``connector`` by ``COPY``-ing an already-mapped BRONZE-shaped SQL relation
    straight to Parquet — no :class:`FocusRecord` objects, no per-row Python at all.
    Returns rows written.

    Used by connectors whose source DuckDB can scan and map directly (see
    :mod:`flashlight.focus.sql_mapping`) — ``aws_focus``. The caller
    owns ``con`` (and closing it); ``mapped_sql`` is materialized into a temp table
    here so its source (e.g. an S3 Parquet scan) is read exactly once, regardless
    of how many times this function itself queries the result.
    """
    _purge_window(connector, window)
    paths.bronze_dir().mkdir(parents=True, exist_ok=True)
    try:
        con.execute(f"CREATE OR REPLACE TEMP TABLE _bronze_batch AS {mapped_sql}")
        assert_single_currency(
            con, "_bronze_batch", connector=connector, base_currency=base_currency
        )
        row = con.execute("SELECT count(*) FROM _bronze_batch").fetchone()
        total = int(row[0]) if row else 0
        if not total:
            logger.info("bronze_window_empty", connector=connector)
            return 0
        con.execute(
            f"COPY _bronze_batch TO '{paths.bronze_dir()}' ({copy_options()})"  # noqa: S608
        )
    except Exception:
        _purge_window(connector, window)
        raise
    finally:
        con.execute("DROP TABLE IF EXISTS _bronze_batch")

    logger.info("bronze_window_written", connector=connector, rows=total)
    return total
