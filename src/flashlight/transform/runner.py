"""Build GOLD Parquet from BRONZE — the "transform" / refresh step.

Backs ``flashlight transform`` and the tail of ``flashlight ingest``. One in-memory
DuckDB reads the BRONZE Parquet as ``raw.focus_record``, applies the SILVER and
GOLD view SQL (``sql/*.sql``), then materializes GOLD **per provider**: each
provider-scoped view is sliced by ``provider_name`` into
``gold/<group>/<view>.parquet``, and the cross-provider views (efficiency/waste,
driver health, policy) go into their own fixed groups. The files are written to a
staging tree and swapped into ``gold/`` atomically per file
(:func:`flashlight.lake.publish.atomic_publish`).

SILVER is never persisted — it lives only as views inside this connection. GOLD
matviews + ``REFRESH … CONCURRENTLY`` are gone: a full rebuild via ``COPY`` is the
refresh, and at single-user data scale it's sub-second.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb

from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.efficiency.policy_rules import build_policy_record_sql
from flashlight.efficiency.waste_rules import build_waste_record_sql
from flashlight.lake import duck, paths
from flashlight.lake.publish import atomic_publish
from flashlight.transform.catalog import (
    AI_USAGE_BASE_VIEWS,
    AI_USAGE_GROUP,
    COMPUTE_BASE_VIEWS,
    COMPUTE_GROUP,
    DRIVER_HEALTH_BASE_VIEWS,
    DRIVER_HEALTH_GROUP,
    EFFICIENCY_BASE_VIEWS,
    EFFICIENCY_GROUP,
    POLICY_BASE_VIEWS,
    POLICY_GROUP,
    PROVIDER_BASE_VIEWS,
    STORAGE_BASE_VIEWS,
    STORAGE_GROUP,
    provider_group,
)

logger = get_logger(__name__)

SQL_DIR = Path(__file__).parent / "sql"


def _statements(sql_text: str) -> list[str]:
    """Split a SQL file into individual ``;``-terminated statements.

    Line comments are stripped first so a ``;`` inside a ``--`` comment isn't
    treated as a terminator. Our SQL has no string literals containing ``--`` or
    ``;``, so this is sufficient.
    """
    decommented = []
    for line in sql_text.splitlines():
        idx = line.find("--")
        decommented.append(line[:idx] if idx != -1 else line)
    cleaned = "\n".join(decommented)
    return [stmt.strip() for stmt in cleaned.split(";") if stmt.strip()]


def _gold_copy_options() -> str:
    settings = get_settings()
    opts = ["FORMAT parquet", f"COMPRESSION '{settings.parquet_compression}'"]
    if settings.parquet_compression == "zstd":
        opts.append(f"COMPRESSION_LEVEL {settings.parquet_compression_level}")
    return ", ".join(opts)


def _sql_quote(value: str) -> str:
    """Escape a string for inlining as a single-quoted SQL literal."""
    return value.replace("'", "''")


def _materialize_sources(con: duckdb.DuckDBPyConnection) -> None:
    """Read each source Parquet root once, into a temp table the views then read.

    SILVER/GOLD are unmaterialized views, and every published file is its own
    ``COPY (SELECT …)`` — so without this each of the ~N×13+10 COPYs re-executes the
    whole view chain down to a fresh Parquet scan. Swapping the source views for
    in-memory temp tables collapses that to one scan per root.

    ponytail: whole-lake materialization, bounded by the connection's memory_limit
    (it spills past that). A window-scoped incremental rebuild is the upgrade path if
    spilling ever costs more than the re-scans did.
    """
    for schema_view, temp in (
        ("raw.focus_record", "_bronze_mat"),
        ("metrics.efficiency_record", "_efficiency_mat"),
        ("metrics.driver_health", "_driver_health_mat"),
        ("metrics.ai_usage", "_ai_usage_mat"),
        ("metrics.storage_location", "_storage_location_mat"),
        ("metrics.compute_instance", "_compute_instance_mat"),
    ):
        con.execute(f"CREATE TEMP TABLE {temp} AS SELECT * FROM {schema_view}")  # noqa: S608
        con.execute(f"CREATE OR REPLACE VIEW {schema_view} AS SELECT * FROM {temp}")  # noqa: S608


def _discover_providers(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Distinct, non-empty ``provider_name`` values present in SILVER.

    The set of provider groups GOLD fans out into is data-driven — whatever
    providers actually appear — so a new provider needs no code change.
    """
    rows = con.execute(
        "SELECT DISTINCT provider_name FROM silver.focus_normalized "
        "WHERE provider_name IS NOT NULL AND provider_name <> '' "
        "ORDER BY provider_name"
    ).fetchall()
    return [r[0] for r in rows]


def build_gold() -> int:
    """Rebuild SILVER (in-memory) and GOLD (Parquet) from BRONZE. Returns files published.

    GOLD is materialized per provider: each provider-scoped view is sliced by
    ``provider_name`` into ``gold/<group>/<view>.parquet`` (one group per provider),
    and the cross-provider views go into the fixed groups below. The in-memory
    ``gold.<view>`` SQL is the shared source for the per-provider COPY slices.
    """
    con = duck.connect()
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS silver")
        con.execute("CREATE SCHEMA IF NOT EXISTS gold")
        duck.register_bronze(con)  # creates schema raw + raw.focus_record
        duck.register_metrics(con)  # creates schema metrics + metrics.efficiency_record
        duck.register_driver_health(con)  # creates metrics.driver_health
        duck.register_ai_usage(con)  # creates metrics.ai_usage
        duck.register_storage_locations(con)  # creates metrics.storage_location
        duck.register_compute_instances(con)  # creates metrics.compute_instance
        _materialize_sources(con)

        # gold.waste_record is config-driven (flashlight.efficiency.waste_rules), not a
        # static .sql file — compiled here so 050_gold_waste.sql's summary rollup can
        # depend on it. See build_waste_record_sql for why this keeps classification
        # deterministic across dashboard/MCP consumers.
        con.execute(build_waste_record_sql())

        # gold.policy_record is the same config-driven pattern (flashlight.efficiency.
        # policy_rules), compiled here so 070_gold_policy.sql's summary rollup can
        # depend on it.
        con.execute(build_policy_record_sql())

        for path in sorted(SQL_DIR.glob("*.sql")):
            for stmt in _statements(path.read_text()):
                con.execute(stmt)
            logger.info("sql_applied", file=path.name)

        staging = paths.gold_staging_dir()
        shutil.rmtree(staging, ignore_errors=True)
        options = _gold_copy_options()
        published = 0

        # Per-provider groups: slice each provider-scoped view by provider_name.
        for provider in _discover_providers(con):
            group = provider_group(provider)
            group_dir = staging / group
            group_dir.mkdir(parents=True, exist_ok=True)
            for spec in PROVIDER_BASE_VIEWS:
                target = group_dir / f"{spec.view}.parquet"
                con.execute(
                    f'COPY (SELECT * FROM gold."{spec.view}" '  # noqa: S608
                    f"WHERE provider_name = '{_sql_quote(provider)}') "
                    f"TO '{target}' ({options})"
                )
                published += 1

        # Fixed cross-provider groups: efficiency/waste + driver health + policy
        # compliance + AI serving usage + backing storage/compute, unfiltered.
        fixed_groups = (
            (EFFICIENCY_GROUP, EFFICIENCY_BASE_VIEWS),
            (DRIVER_HEALTH_GROUP, DRIVER_HEALTH_BASE_VIEWS),
            (POLICY_GROUP, POLICY_BASE_VIEWS),
            (AI_USAGE_GROUP, AI_USAGE_BASE_VIEWS),
            (STORAGE_GROUP, STORAGE_BASE_VIEWS),
            (COMPUTE_GROUP, COMPUTE_BASE_VIEWS),
        )
        for group, specs in fixed_groups:
            group_dir = staging / group
            group_dir.mkdir(parents=True, exist_ok=True)
            for spec in specs:
                target = group_dir / f"{spec.view}.parquet"
                con.execute(
                    f'COPY (SELECT * FROM gold."{spec.view}") TO \'{target}\' ({options})'  # noqa: S608
                )
                published += 1
    finally:
        con.close()

    atomic_publish(paths.gold_staging_dir(), paths.gold_dir())
    logger.info("gold_built", files=published)
    return published


def apply_views(rebuild: bool = False) -> int:
    """Back-compat alias for :func:`build_gold`. Every build is a full rebuild now,
    so ``rebuild`` is a no-op."""
    return build_gold()
