"""In-memory DuckDB connections over the Parquet lake.

Every process — ingest (writer), MCP, dashboard — calls :func:`connect` to get a
throwaway in-memory DuckDB, then registers the lake relations it needs:

* :func:`register_bronze` exposes ``raw.focus_record`` over the partitioned BRONZE
  Parquet (with a typed empty fallback before the first ingest), for the transform
  to build SILVER/GOLD from.
* :func:`register_gold` exposes ``<group>.<view>`` over each published GOLD Parquet
  (a schema per provider group, plus ``shared``), for the read surface (MCP,
  dashboard) to query.

DuckDB is just the compute engine here — it owns no files on disk, so any number
of these connections can run concurrently against the same immutable Parquet.
"""

from __future__ import annotations

import duckdb

from flashlight.lake import paths
from flashlight.lake.driver_health_schema import empty_table as empty_driver_health_table
from flashlight.lake.metrics_schema import empty_table as empty_metrics_table
from flashlight.lake.schema import empty_table


def connect() -> duckdb.DuckDBPyConnection:
    """A fresh in-memory DuckDB. JSON functions are built in (autoloaded).

    Pins ``TimeZone='UTC'`` — DuckDB's default session timezone is the *host
    machine's* local zone, not UTC. Every SILVER/GOLD SQL file that does
    ``date_trunc('month'/'day', charge_period_start)`` on a TIMESTAMPTZ column
    converts it to the session timezone first; without pinning UTC, a charge
    timestamped exactly at UTC midnight on the 1st gets shifted back into the
    *previous* day/month on any host west of UTC (confirmed: a Chicago host
    truncates a 2026-07-01T00:00:00Z charge to 2026-06-01). Same reasoning as
    ``sql_mapping.ensure_helpers``'s identical pin on the ingest-time connection —
    this is the shared factory transform/dashboard/MCP all read through, so it
    needs the same pin.
    """
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone='UTC'")
    return con


def register_bronze(con: duckdb.DuckDBPyConnection) -> None:
    """Expose ``raw.focus_record`` over the partitioned BRONZE Parquet.

    Reads with ``hive_partitioning`` so ``x_source_connector`` and ``charge_month``
    (written as directory names) come back as columns. Before the first ingest
    there are no files, so we fall back to a typed empty table — the column set is
    identical either way, so the SILVER/GOLD SQL resolves against both.
    """
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    files = list(paths.bronze_dir().glob("**/*.parquet"))
    if files:
        # CREATE VIEW can't be prepared, so the path is inlined (single-quote escaped).
        glob = str(paths.bronze_dir() / "**" / "*.parquet").replace("'", "''")
        con.execute(
            f"CREATE OR REPLACE VIEW raw.focus_record AS SELECT * FROM "
            f"read_parquet('{glob}', hive_partitioning=true, union_by_name=true)"
        )
    else:
        con.register("_bronze_empty", empty_table())
        con.execute(
            "CREATE OR REPLACE VIEW raw.focus_record AS SELECT * FROM _bronze_empty"
        )


def register_metrics(con: duckdb.DuckDBPyConnection) -> None:
    """Expose ``metrics.efficiency_record`` over the partitioned metrics Parquet.

    The waste-plane sibling of :func:`register_bronze`: reads with
    ``hive_partitioning`` so ``provider_name`` and ``charge_month`` come back as
    columns, with the same typed-empty fallback before the first efficiency pull so
    the GOLD waste SQL resolves either way.
    """
    con.execute("CREATE SCHEMA IF NOT EXISTS metrics")
    files = list(paths.metrics_dir().glob("**/*.parquet"))
    if files:
        glob = str(paths.metrics_dir() / "**" / "*.parquet").replace("'", "''")
        con.execute(
            f"CREATE OR REPLACE VIEW metrics.efficiency_record AS SELECT * FROM "
            f"read_parquet('{glob}', hive_partitioning=true, union_by_name=true)"
        )
    else:
        con.register("_metrics_empty", empty_metrics_table())
        con.execute(
            "CREATE OR REPLACE VIEW metrics.efficiency_record AS SELECT * FROM _metrics_empty"
        )


def register_driver_health(con: duckdb.DuckDBPyConnection) -> None:
    """Expose ``metrics.driver_health`` over the partitioned driver-health Parquet.

    Same ``metrics`` DuckDB schema as :func:`register_metrics`, but a distinct Parquet
    root (:func:`~flashlight.lake.paths.driver_health_dir`) and view name — kept out of
    ``metrics_dir()``'s own recursive glob so the two differently-shaped datasets never
    collide (see that function's docstring).
    """
    con.execute("CREATE SCHEMA IF NOT EXISTS metrics")
    files = list(paths.driver_health_dir().glob("**/*.parquet"))
    if files:
        glob = str(paths.driver_health_dir() / "**" / "*.parquet").replace("'", "''")
        con.execute(
            f"CREATE OR REPLACE VIEW metrics.driver_health AS SELECT * FROM "
            f"read_parquet('{glob}', hive_partitioning=true, union_by_name=true)"
        )
    else:
        con.register("_driver_health_empty", empty_driver_health_table())
        con.execute(
            "CREATE OR REPLACE VIEW metrics.driver_health AS SELECT * FROM _driver_health_empty"
        )


def register_gold(con: duckdb.DuckDBPyConnection) -> None:
    """Expose published GOLD Parquet as ``<group>.<view>`` views, one schema per group.

    GOLD is laid out ``gold/<group>/<view>.parquet`` (a group per provider, plus
    ``shared`` for TCO); each group dir becomes a DuckDB schema. Only files that
    exist are registered; before the first transform there are no group dirs, so
    queries against a missing view surface as 'unknown view'.
    """
    for group_dir in sorted(p for p in paths.gold_dir().glob("*") if p.is_dir()):
        group = group_dir.name
        con.execute(f'CREATE SCHEMA IF NOT EXISTS "{group}"')
        for parquet in sorted(group_dir.glob("*.parquet")):
            view = parquet.stem
            path = str(parquet).replace("'", "''")
            con.execute(
                f'CREATE OR REPLACE VIEW "{group}"."{view}" '
                f"AS SELECT * FROM read_parquet('{path}')"
            )
