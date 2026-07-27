"""Vectorized FOCUS-CSV -> BRONZE Parquet loader, for ``flashlight sample``.

Delegates the row mapping entirely to :mod:`flashlight.focus.sql_mapping` — the
same SQL projection ``aws_focus``/``focus_file`` use — so a sample seed and a real
connector pull are mapped by exactly one set of rules, never two. This module only
adds the CSV-specific read expression and its own purge semantics: a full
connector-replace (:func:`flashlight.lake.bronze.purge_connector`) rather than a
window-scoped one, since sample/backfill data isn't naturally windowed the way a
real ingest run is.
"""

from __future__ import annotations

from pathlib import Path

from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.focus import sql_mapping
from flashlight.lake import bronze, duck, paths

logger = get_logger(__name__)


def seed_from_csv(csv_path: Path, *, connector: str, ingest_run_id: str) -> int:
    """Load a FOCUS CSV into BRONZE set-based (full replace for *connector*). Returns rows.

    Asserts a single billing currency matching ``FLASHLIGHT_BASE_CURRENCY`` — the same
    mixed-currency guard the connector path enforces.
    """
    settings = get_settings()
    path = str(csv_path).replace("'", "''")
    source_sql = f"read_csv('{path}', header=true, all_varchar=true)"

    con = duck.connect()
    try:
        sql_mapping.ensure_helpers(con)
        present = sql_mapping.present_columns(con, source_sql)
        mapped = sql_mapping.mapping_sql(
            source_sql, connector=connector, run_id=ingest_run_id, present=present
        )
        con.execute(f"CREATE OR REPLACE TEMP TABLE _seed AS {mapped}")
        sql_mapping.assert_single_currency(
            con, "_seed", connector=connector, base_currency=settings.base_currency
        )

        result = con.execute("SELECT count(*) FROM _seed").fetchone()
        count = int(result[0]) if result else 0
        if count == 0:
            logger.info("seed_empty", connector=connector)
            return 0

        bronze.purge_connector(connector)
        paths.bronze_dir().mkdir(parents=True, exist_ok=True)
        con.execute(
            f"COPY _seed TO '{paths.bronze_dir()}' ({bronze.copy_options()})"  # noqa: S608
        )
    finally:
        con.close()
    logger.info("seed_written", connector=connector, rows=count)
    return count
