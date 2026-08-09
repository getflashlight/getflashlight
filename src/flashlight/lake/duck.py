"""In-memory DuckDB connections over the Parquet lake.

Every process — ingest (writer), MCP, dashboard — calls :func:`connect` to get a
throwaway in-memory DuckDB, then registers the lake relations it needs:

* :func:`register_bronze` exposes ``raw.focus_record`` over the partitioned BRONZE
  Parquet (with a typed empty fallback before the first ingest), for the transform
  to build SILVER/GOLD from.
* :func:`register_gold` exposes ``<group>.<view>`` over each published GOLD Parquet
  (a schema per provider group, plus the fixed cross-provider groups), for the read
  surface (MCP, dashboard) to query.

DuckDB is just the compute engine here — it owns no files on disk, so any number
of these connections can run concurrently against the same immutable Parquet.
"""

from __future__ import annotations

import duckdb

from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.lake import paths
from flashlight.lake.ai_usage_schema import empty_table as empty_ai_usage_table
from flashlight.lake.assistant_turns import empty_table as empty_assistant_turn_table
from flashlight.lake.compute_instance_schema import empty_table as empty_compute_instance_table
from flashlight.lake.driver_health_schema import empty_table as empty_driver_health_table
from flashlight.lake.metrics_schema import empty_table as empty_metrics_table
from flashlight.lake.redshift_policy_config_schema import (
    empty_table as empty_redshift_policy_config_table,
)
from flashlight.lake.redshift_table_observability_schema import (
    empty_table as empty_redshift_table_observability_table,
)
from flashlight.lake.schema import empty_table
from flashlight.lake.storage_location_schema import empty_table as empty_storage_location_table

logger = get_logger(__name__)

# connect() runs on every query path, so an unwritable spill dir would otherwise log
# once per query. Warn once per process instead.
_temp_dir_warned = False


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

    Also caps memory and points spill at the lake home: DuckDB defaults to ~80% of
    system RAM per connection, and this is a laptop tool where several connections
    (ingest workers, a transform, the dashboard) can be live at once.

    The spill dir is best-effort. If it can't be created — a read-only lake home, e.g. a
    container run with ``--read-only`` and no ``FLASHLIGHT_DUCKDB_TEMP_DIR`` — we warn once
    and leave ``temp_directory`` unset rather than raising. That trades a *total* outage
    (every query fails, because connect() is on every read path) for a bounded one (only a
    query large enough to spill fails). Since this is also the read path for a read-only
    demo, whose queries are far too small to spill, the practical effect there is none.
    """
    global _temp_dir_warned
    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone='UTC'")
    con.execute(f"SET memory_limit='{get_settings().duckdb_memory_limit}'")
    temp_dir = paths.duckdb_temp_dir()
    try:
        temp_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        if not _temp_dir_warned:
            _temp_dir_warned = True
            logger.warning(
                "duckdb_temp_dir_unwritable",
                path=str(temp_dir),
                error=str(exc),
                hint="set FLASHLIGHT_DUCKDB_TEMP_DIR to a writable path; queries that "
                "exceed the memory limit will fail until then",
            )
    else:
        con.execute(f"SET temp_directory='{str(temp_dir).replace(chr(39), chr(39) * 2)}'")
    return con


def register_bronze(con: duckdb.DuckDBPyConnection) -> None:
    """Expose ``raw.focus_record`` over the partitioned BRONZE Parquet.

    Reads with ``hive_partitioning`` so ``x_source_connector`` and ``charge_month``
    (written as directory names) come back as columns. Before the first ingest
    there are no files, so we fall back to a typed empty table — the column set is
    identical either way, so the SILVER/GOLD SQL resolves against both.
    """
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    files = list(paths.bronze_dir().glob("x_source_connector=*/charge_month=*/*.parquet"))
    if files:
        # CREATE VIEW can't be prepared, so the path is inlined (single-quote escaped).
        glob = str(
            paths.bronze_dir() / "x_source_connector=*" / "charge_month=*" / "*.parquet"
        ).replace("'", "''")
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
    columns, unioned onto the typed empty table so the view's schema is always the
    *current* one regardless of what's on disk — see :func:`register_assistant_turns`'s
    docstring for why ``UNION ALL BY NAME`` against the empty table (not just an
    if/else fallback) matters: a column added to :mod:`flashlight.lake.metrics_schema`
    after older EfficiencyRecord Parquet was written would otherwise make every GOLD
    waste SQL statement referencing it fail outright, for every provider, until a fresh
    pull happens to write at least one new-schema file.
    """
    con.execute("CREATE SCHEMA IF NOT EXISTS metrics")
    con.register("_metrics_empty", empty_metrics_table())
    select = "SELECT * FROM _metrics_empty"
    files = list(paths.metrics_dir().glob("**/*.parquet"))
    if files:
        glob = str(paths.metrics_dir() / "**" / "*.parquet").replace("'", "''")
        select += (
            " UNION ALL BY NAME SELECT * FROM "
            f"read_parquet('{glob}', hive_partitioning=true, union_by_name=true)"
        )
    con.execute(f"CREATE OR REPLACE VIEW metrics.efficiency_record AS {select}")


def register_driver_health(con: duckdb.DuckDBPyConnection) -> None:
    """Expose typed driver-health Bronze as ``raw.driver_health``.

    New Bronze partitions win over the legacy sibling dataset for the same
    provider/month, preserving old history until it is replaced by a fresh ingest.
    """
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.register("_driver_health_empty", empty_driver_health_table())
    primary = "SELECT * FROM _driver_health_empty"
    if list(paths.bronze_driver_health_dir().glob("**/*.parquet")):
        glob = str(paths.bronze_driver_health_dir() / "**" / "*.parquet").replace("'", "''")
        primary = (
            "SELECT * FROM _driver_health_empty UNION ALL BY NAME SELECT * FROM "
            f"read_parquet('{glob}', hive_partitioning=true, union_by_name=true)"
        )
    legacy = "SELECT * FROM _driver_health_empty"
    if list(paths.legacy_driver_health_dir().glob("**/*.parquet")):
        glob = str(paths.legacy_driver_health_dir() / "**" / "*.parquet").replace("'", "''")
        legacy = (
            "SELECT * FROM _driver_health_empty UNION ALL BY NAME SELECT * FROM "
            f"read_parquet('{glob}', hive_partitioning=true, union_by_name=true)"
        )
    con.execute(
        "CREATE OR REPLACE VIEW raw.driver_health AS "
        f"WITH primary_rows AS ({primary}), legacy_rows AS ({legacy}) "
        "SELECT * FROM primary_rows UNION ALL BY NAME "
        "SELECT l.* FROM legacy_rows l WHERE NOT EXISTS ("
        "SELECT 1 FROM primary_rows p WHERE p.provider_name = l.provider_name "
        "AND p.charge_month = l.charge_month)"
    )


def register_redshift_policy_config(con: duckdb.DuckDBPyConnection) -> None:
    """Expose local typed Bronze Redshift controls as ``raw.redshift_policy_config``."""
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.register("_redshift_policy_config_empty", empty_redshift_policy_config_table())
    select = "SELECT * FROM _redshift_policy_config_empty"
    if list(paths.redshift_policy_config_dir().glob("**/*.parquet")):
        glob = str(paths.redshift_policy_config_dir() / "**" / "*.parquet").replace("'", "''")
        select += (
            f" UNION ALL BY NAME SELECT * FROM "
            f"read_parquet('{glob}', hive_partitioning=true, union_by_name=true)"
        )
    con.execute(f"CREATE OR REPLACE VIEW raw.redshift_policy_config AS {select}")


def register_redshift_table_observability(con: duckdb.DuckDBPyConnection) -> None:
    """Expose durable daily Redshift table and Spectrum facts in ``raw``."""
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.register("_redshift_table_observability_empty", empty_redshift_table_observability_table())
    select = "SELECT * FROM _redshift_table_observability_empty"
    if list(paths.redshift_table_observability_dir().glob("**/*.parquet")):
        glob = str(paths.redshift_table_observability_dir() / "**" / "*.parquet").replace("'", "''")
        select += (
            " UNION ALL BY NAME SELECT * FROM "
            f"read_parquet('{glob}', hive_partitioning=true, union_by_name=true)"
        )
    con.execute(f"CREATE OR REPLACE VIEW raw.redshift_table_observability AS {select}")


def register_ai_usage(con: duckdb.DuckDBPyConnection) -> None:
    """Expose ``metrics.ai_usage`` over the partitioned AI serving-usage Parquet.

    Same ``metrics`` DuckDB schema and typed-empty-union fallback as
    :func:`register_driver_health`, over its own Parquet root
    (:func:`~flashlight.lake.paths.ai_usage_dir`) for the same
    keep-out-of-the-recursive-glob reason.
    """
    con.execute("CREATE SCHEMA IF NOT EXISTS metrics")
    con.register("_ai_usage_empty", empty_ai_usage_table())
    select = "SELECT * FROM _ai_usage_empty"
    files = list(paths.ai_usage_dir().glob("**/*.parquet"))
    if files:
        glob = str(paths.ai_usage_dir() / "**" / "*.parquet").replace("'", "''")
        select += (
            " UNION ALL BY NAME SELECT * FROM "
            f"read_parquet('{glob}', hive_partitioning=true, union_by_name=true)"
        )
    con.execute(f"CREATE OR REPLACE VIEW metrics.ai_usage AS {select}")


def register_storage_locations(con: duckdb.DuckDBPyConnection) -> None:
    """Expose ``metrics.storage_location`` over the partitioned storage-location Parquet.

    Same ``metrics`` DuckDB schema and typed-empty fallback as
    :func:`register_driver_health`, over its own Parquet root
    (:func:`~flashlight.lake.paths.storage_locations_dir`) for the same
    keep-out-of-the-recursive-glob reason. Its partition keys are
    ``provider_name``/``snapshot_month`` — the second is deliberately not a charge
    period (see :mod:`flashlight.lake.storage_location_schema`).

    The typed-empty-union fallback is what lets ``065_gold_storage.sql`` resolve before
    any Databricks connection has ever run: without it, an AWS-only lake would fail the
    whole transform rather than publishing an empty map — and, per
    :func:`register_metrics`'s docstring, unioning (not if/else-ing) it also means a
    column added to :mod:`flashlight.lake.storage_location_schema` after older Parquet
    was written doesn't fail every SQL statement after it until a fresh pull happens to
    land at least one new-schema file (see ``cluster_name``/``owner_user`` landing in
    :mod:`flashlight.lake.compute_instance_schema` for the real incident this guards).
    """
    con.execute("CREATE SCHEMA IF NOT EXISTS metrics")
    con.register("_storage_location_empty", empty_storage_location_table())
    select = "SELECT * FROM _storage_location_empty"
    files = list(paths.storage_locations_dir().glob("**/*.parquet"))
    if files:
        glob = str(paths.storage_locations_dir() / "**" / "*.parquet").replace("'", "''")
        select += (
            " UNION ALL BY NAME SELECT * FROM "
            f"read_parquet('{glob}', hive_partitioning=true, union_by_name=true)"
        )
    con.execute(f"CREATE OR REPLACE VIEW metrics.storage_location AS {select}")


def register_compute_instances(con: duckdb.DuckDBPyConnection) -> None:
    """Expose ``metrics.compute_instance`` over the partitioned compute-instance Parquet.

    Same ``metrics`` DuckDB schema and typed-empty fallback as
    :func:`register_driver_health`, over its own Parquet root
    (:func:`~flashlight.lake.paths.compute_instances_dir`) for the same
    keep-out-of-the-recursive-glob reason. Its partition keys are
    ``provider_name``/``charge_month`` — a real charge period, unlike
    :func:`register_storage_locations`'s snapshot (see
    :mod:`flashlight.lake.compute_instance_schema`).

    The typed-empty-union fallback is what lets ``066_gold_compute.sql`` resolve before
    any Databricks connection has ever run: without it, an AWS-only lake would fail the
    whole transform rather than publishing an empty map. It's ``UNION ALL BY NAME``
    against the empty table, not an if/else on it, for the same reason
    :func:`register_metrics` gives — this is in fact the exact plane that motivated
    that fix: ``cluster_name``/``owner_user`` were added to
    :mod:`flashlight.lake.compute_instance_schema` after real Parquet had already been
    written without them, and an if/else fallback (only used when there are *zero*
    files) doesn't help once there's *some* real, older-schema data on disk — every SQL
    statement referencing the new columns failed, not just the compute ones, because
    ``flashlight transform`` applies every ``.sql`` file in one connection and a bad
    statement aborts the run before later files ever get a chance to run.
    """
    con.execute("CREATE SCHEMA IF NOT EXISTS metrics")
    con.register("_compute_instance_empty", empty_compute_instance_table())
    select = "SELECT * FROM _compute_instance_empty"
    files = list(paths.compute_instances_dir().glob("**/*.parquet"))
    if files:
        glob = str(paths.compute_instances_dir() / "**" / "*.parquet").replace("'", "''")
        select += (
            " UNION ALL BY NAME SELECT * FROM "
            f"read_parquet('{glob}', hive_partitioning=true, union_by_name=true)"
        )
    con.execute(f"CREATE OR REPLACE VIEW metrics.compute_instance AS {select}")


def register_assistant_turns(con: duckdb.DuckDBPyConnection) -> None:
    """Expose ``telemetry.assistant_turn`` over the BYOK assistant usage log.

    Unlike :func:`register_metrics`/:func:`register_driver_health`, these files
    aren't Hive-partitioned by directory — each is just one ``<turn_id>.parquet``
    under :func:`~flashlight.lake.paths.assistant_turns_dir` — so no
    ``hive_partitioning`` flag is needed, just a flat glob.

    The log is append-only *and* long-lived, so files written by older versions
    of Flashlight sit alongside new ones forever — and
    :data:`~flashlight.lake.assistant_turns.ASSISTANT_TURN_SCHEMA` grows over time
    (per-turn latency columns were added after the token columns). Two things
    make that safe, and both are load-bearing:

    * ``union_by_name=true`` — without it DuckDB rejects a glob whose files
      disagree on columns, so one new-schema turn would break the whole
      ``/usage`` page for every older turn already on disk.
    * unioning onto the typed empty table — that pins the view's schema to the
      *current* one no matter what's on disk, so ``views/usage.py`` can name a
      new column in its SELECT and still work against a lake holding only
      old-schema files. ``UNION ALL BY NAME`` fills the absent columns with NULL.
    """
    con.execute("CREATE SCHEMA IF NOT EXISTS telemetry")
    con.register("_assistant_turn_empty", empty_assistant_turn_table())
    dirs = (paths.assistant_turns_dir(), paths.legacy_assistant_turns_dir())
    globs = [str(d / "*.parquet") for d in dirs if any(d.glob("*.parquet"))]
    select = "SELECT * FROM _assistant_turn_empty"
    if globs:
        quoted = ", ".join("'" + g.replace("'", "''") + "'" for g in globs)
        select += (
            f" UNION ALL BY NAME SELECT * FROM read_parquet([{quoted}], union_by_name=true)"
        )
    con.execute(f"CREATE OR REPLACE VIEW telemetry.assistant_turn AS {select}")


def register_gold(con: duckdb.DuckDBPyConnection) -> None:
    """Expose published GOLD Parquet as ``<group>.<view>`` views, one schema per group.

    GOLD is laid out ``gold/<group>/<view>.parquet`` (a group per provider, plus the
    fixed cross-provider groups); each group dir becomes a DuckDB schema. Only files that
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
