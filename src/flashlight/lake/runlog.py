"""Ingest run log — the lightweight replacement for the ``meta.ingest_run`` table.

Each connector run appends one Parquet file under ``meta/runs/`` (one file per
run keeps it append-only and free of read-modify-write races). It's pure
observability — nothing in the metrics path reads it — so it stays out of the
way of the lake's hot path.
"""

from __future__ import annotations

from datetime import datetime

import pyarrow as pa

from flashlight.core.logging import get_logger
from flashlight.lake import paths

logger = get_logger(__name__)

_TS = pa.timestamp("us", tz="UTC")
RUN_SCHEMA: pa.Schema = pa.schema(
    [
        ("run_id", pa.string()),
        ("connector", pa.string()),
        ("status", pa.string()),  # success | failed
        ("rows", pa.int64()),
        ("detail", pa.string()),
        ("started_at", _TS),
        ("finished_at", _TS),
    ]
)


def record_run(
    *,
    run_id: str,
    connector: str,
    status: str,
    rows: int,
    started_at: datetime,
    finished_at: datetime,
    detail: str | None = None,
) -> None:
    """Append one connector run to the log (best-effort; never raises)."""
    try:
        import pyarrow.parquet as pq

        paths.runs_dir().mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(
            [
                {
                    "run_id": run_id,
                    "connector": connector,
                    "status": status,
                    "rows": rows,
                    "detail": detail,
                    "started_at": started_at,
                    "finished_at": finished_at,
                }
            ],
            schema=RUN_SCHEMA,
        )
        pq.write_table(  # type: ignore[no-untyped-call]
            table, paths.runs_dir() / f"{run_id}-{connector}.parquet"
        )
    except Exception as exc:  # noqa: BLE001 - observability must not break ingest
        logger.warning("runlog_write_failed", connector=connector, error=str(exc))
