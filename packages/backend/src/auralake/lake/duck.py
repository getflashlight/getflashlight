"""In-memory DuckDB connections over the Parquet lake.

Every process — ingest (writer), MCP, dashboard — calls :func:`connect` to get a
throwaway in-memory DuckDB, then registers the lake relations it needs:

* :func:`register_bronze` exposes ``raw.focus_record`` over the partitioned BRONZE
  Parquet (with a typed empty fallback before the first ingest), for the transform
  to build SILVER/GOLD from.
* :func:`register_gold` exposes ``gold.<view>`` over each published GOLD Parquet,
  for the read surface (MCP, dashboard) to query.

DuckDB is just the compute engine here — it owns no files on disk, so any number
of these connections can run concurrently against the same immutable Parquet.
"""

from __future__ import annotations

import duckdb

from auralake.lake import paths
from auralake.lake.schema import empty_table


def connect() -> duckdb.DuckDBPyConnection:
    """A fresh in-memory DuckDB. JSON functions are built in (autoloaded)."""
    return duckdb.connect(":memory:")


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


def register_gold(con: duckdb.DuckDBPyConnection) -> None:
    """Expose each published GOLD Parquet as a ``gold.<view>`` view.

    Only files that exist are registered; before the first transform the schema is
    simply empty and queries against a missing view surface as 'unknown view'.
    """
    con.execute("CREATE SCHEMA IF NOT EXISTS gold")
    for parquet in sorted(paths.gold_dir().glob("*.parquet")):
        view = parquet.stem
        path = str(parquet).replace("'", "''")
        con.execute(
            f"CREATE OR REPLACE VIEW gold.\"{view}\" AS SELECT * FROM read_parquet('{path}')"
        )
