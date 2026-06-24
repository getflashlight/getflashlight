"""Build GOLD Parquet from BRONZE — the "transform" / refresh step.

Backs ``auralake transform`` and the tail of ``auralake ingest``. One in-memory
DuckDB reads the BRONZE Parquet as ``raw.focus_record``, applies the SILVER and
GOLD view SQL (``sql/*.sql``), then materializes each GOLD view to a zstd Parquet
file via ``COPY``. The files are written to a staging dir and swapped into ``gold/``
atomically per file (:func:`auralake.lake.publish.atomic_publish`).

SILVER is never persisted — it lives only as views inside this connection. GOLD
matviews + ``REFRESH … CONCURRENTLY`` are gone: a full rebuild via ``COPY`` is the
refresh, and at single-user data scale it's sub-second.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from auralake.core.logging import get_logger
from auralake.core.settings import get_settings
from auralake.lake import duck, paths
from auralake.lake.publish import atomic_publish
from auralake.transform.catalog import CATALOG

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


def build_gold() -> int:
    """Rebuild SILVER (in-memory) and GOLD (Parquet) from BRONZE. Returns views published."""
    con = duck.connect()
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS silver")
        con.execute("CREATE SCHEMA IF NOT EXISTS gold")
        duck.register_bronze(con)  # creates schema raw + raw.focus_record

        for path in sorted(SQL_DIR.glob("*.sql")):
            for stmt in _statements(path.read_text()):
                con.execute(stmt)
            logger.info("sql_applied", file=path.name)

        staging = paths.gold_staging_dir()
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        options = _gold_copy_options()
        for view in CATALOG:
            short = view.name.removeprefix("gold.")
            target = staging / f"{short}.parquet"
            con.execute(
                f'COPY (SELECT * FROM gold."{short}") TO \'{target}\' ({options})'  # noqa: S608
            )
    finally:
        con.close()

    published = atomic_publish(paths.gold_staging_dir(), paths.gold_dir())
    logger.info("gold_built", views=published)
    return published


def apply_views(rebuild: bool = False) -> int:
    """Back-compat alias for :func:`build_gold`. Every build is a full rebuild now,
    so ``rebuild`` is a no-op."""
    return build_gold()
