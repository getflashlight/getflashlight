"""On-disk layout for the Parquet lake — the single source of truth for *where*
persistent data lives. Everything hangs off :func:`home`, so the writer and the
readers resolve identical paths.
"""

from __future__ import annotations

from pathlib import Path

import platformdirs

from flashlight.core.settings import get_settings


def home() -> Path:
    """Lake root: ``FLASHLIGHT_HOME`` if set, else the platform user-data dir."""
    configured = get_settings().home
    if configured:
        return Path(configured).expanduser()
    return Path(platformdirs.user_data_dir("flashlight"))


def config_dir() -> Path:
    return home() / "config"


def connections_path() -> Path:
    """Default connector config location (``flashlight init`` writes here)."""
    return config_dir() / "connections.yml"


def policies_path() -> Path:
    """Optional cost-policy threshold overrides (``flashlight init`` scaffolds it).

    Absent means "use the efficient defaults" — see
    :mod:`flashlight.efficiency.policy_config`.
    """
    return config_dir() / "policies.yml"


def bronze_dir() -> Path:
    """BRONZE root, Hive-partitioned ``x_source_connector=…/charge_month=…/``."""
    return home() / "bronze"


def metrics_dir() -> Path:
    """Efficiency-telemetry root, Hive-partitioned ``provider_name=…/charge_month=…/``.

    The waste-plane sibling of :func:`bronze_dir` — holds the aggregated
    ``EfficiencyRecord`` rows the GOLD waste view classifies. Separate from BRONZE
    because efficiency telemetry does not fit ``FocusRecord``.
    """
    return home() / "metrics"


def driver_health_dir() -> Path:
    """Driver-health telemetry root, Hive-partitioned ``provider_name=…/charge_month=…/``.

    A sibling of :func:`metrics_dir`, not nested inside it: ``duck.register_metrics``
    globs ``metrics_dir()/**/*.parquet`` recursively with ``union_by_name=true`` — a
    differently-shaped dataset nested inside that tree would silently corrupt that
    glob/view. Fleet-health/compliance data (client driver versions), not waste.
    """
    return home() / "driver_health"


def gold_dir() -> Path:
    """GOLD root — one ``<view>.parquet`` per catalogued metric (consumer surface)."""
    return home() / "gold"


def gold_signature() -> tuple[tuple[str, int], ...]:
    """Identity of the current GOLD files (relpath + mtime) — changes on every publish.

    Keyed on the path relative to ``gold/`` so two groups' identically-named files
    (e.g. ``aws/monthly_bill.parquet`` / ``databricks/monthly_bill.parquet``) don't
    collide. Readers rebuild their cached connection when this changes.
    """
    gold = gold_dir()
    return tuple(
        sorted(
            (p.relative_to(gold).as_posix(), p.stat().st_mtime_ns)
            for p in gold.glob("*/*.parquet")
        )
    )


def duckdb_temp_dir() -> Path:
    """Spill dir for DuckDB once a query exceeds ``FLASHLIGHT_DUCKDB_MEMORY_LIMIT``.

    Under the lake home rather than the system temp dir so a large transform spills
    onto the same volume the user already gave us space on.
    """
    return home() / "tmp" / "duckdb"


def gold_staging_dir() -> Path:
    """Transient dir a transform builds GOLD into before the atomic publish swap."""
    return home() / "gold.staging"


def meta_dir() -> Path:
    return home() / "meta"


def runs_dir() -> Path:
    """Ingest run log — one Parquet file per run (append-only, concurrency-safe)."""
    return meta_dir() / "runs"


def sync_logs_dir() -> Path:
    """Saved sync transcripts — one text file per whole ``run_ingest()`` call, named
    by its shared ``run_id`` (see :mod:`flashlight.lake.runlog`). Written by the
    dashboard's :func:`flashlight.dashboard.ingest_runner.stream_sync` as it tails
    a sync subprocess, so a run's log survives closing the dialog that started it.
    """
    return meta_dir() / "sync_logs"


def sync_log_path(run_id: str) -> Path:
    return sync_logs_dir() / f"{run_id}.log"


def chat_turns_dir() -> Path:
    """BYOK chat usage log — one Parquet file per chat turn (append-only)."""
    return meta_dir() / "chat_turns"


def ensure_layout() -> None:
    """Create the lake directory skeleton (idempotent)."""
    for path in (
        config_dir(),
        bronze_dir(),
        metrics_dir(),
        driver_health_dir(),
        gold_dir(),
        runs_dir(),
        sync_logs_dir(),
        chat_turns_dir(),
    ):
        path.mkdir(parents=True, exist_ok=True)
