"""Ingest run log — the lightweight replacement for the ``meta.ingest_run`` table.

Each connector run appends one Parquet file under ``meta/runs/`` (one file per
run keeps it append-only and free of read-modify-write races). It's pure
observability — nothing in the metrics path reads it — so it stays out of the
way of the lake's hot path.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
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
        pq.write_table(table, paths.runs_dir() / f"{run_id}-{connector}.parquet")
    except Exception as exc:  # noqa: BLE001 - observability must not break ingest
        logger.warning("runlog_write_failed", connector=connector, error=str(exc))


def read_runs(limit: int = 50) -> pd.DataFrame:
    """Most recent ``limit`` connector runs, newest first. Empty if none logged yet."""
    files = sorted(paths.runs_dir().glob("*.parquet"))
    if not files:
        return pd.DataFrame(columns=[f.name for f in RUN_SCHEMA])
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    return df.sort_values("finished_at", ascending=False).head(limit).reset_index(drop=True)


GROUP_COLUMNS = ["run_id", "started_at", "finished_at", "rows", "connectors", "status"]


def read_run_groups(limit: int = 20) -> pd.DataFrame:
    """One row per whole sync (every connector sharing a ``run_id``), newest first —
    the dashboard's per-run history view. Aggregates :func:`read_runs`'s
    per-connector rows: the run's overall ``[min started_at, max finished_at]``
    span, total rows, connector count, and a worst-of ``status`` ("failed" if any
    connector in the run failed, else "success").

    Reads a wide connector-row window (well beyond ``limit`` runs' worth) before
    grouping, so a run isn't silently split across a truncation boundary — a
    ``run_id`` only ever has a handful of connector rows, so this stays cheap.
    Runs logged before ``run_id`` became a shared-per-sync id (each connector had
    its own) degrade gracefully: each of those old rows is its own one-connector
    group, not merged with anything.
    """
    df = read_runs(limit=max(limit * 20, 200))
    if df.empty:
        return pd.DataFrame(columns=GROUP_COLUMNS)
    grouped = (
        df.groupby("run_id", as_index=False)
        .agg(
            started_at=("started_at", "min"),
            finished_at=("finished_at", "max"),
            rows=("rows", "sum"),
            connectors=("connector", "count"),
            failed=("status", lambda s: bool((s == "failed").any())),
        )
        .assign(status=lambda d: d["failed"].map({True: "failed", False: "success"}))
        .drop(columns=["failed"])
    )
    return (
        grouped.sort_values("finished_at", ascending=False).head(limit).reset_index(drop=True)
    )
