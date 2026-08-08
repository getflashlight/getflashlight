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


def assistant_config_path() -> Path:
    """BYOK assistant model choice — provider / model / base URL, no secret.

    Sits beside ``connections.yml`` and ``policies.yml`` so the whole of a user's
    configuration is in one directory they can back up or mount into a container.
    Absent means "use the UI's preset defaults" — see
    :mod:`flashlight.dashboard.assistant_config`.
    """
    return config_dir() / "assistant.yml"


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


def ai_usage_dir() -> Path:
    """AI serving-usage telemetry root, Hive-partitioned ``provider_name=…/charge_month=…/``.

    A sibling of :func:`metrics_dir` for the same reason :func:`driver_health_dir` is —
    ``duck.register_metrics`` globs ``metrics_dir()/**/*.parquet`` recursively with
    ``union_by_name=true``, so a differently-shaped dataset nested in that tree would
    silently corrupt that view. Token/request measurement per served model and requester,
    not waste and not cost (the endpoint's dollars stay in the FOCUS plane).
    """
    return home() / "ai_usage"


def storage_locations_dir() -> Path:
    """Unity Catalog storage-location root, Hive-partitioned
    ``provider_name=…/snapshot_month=…/``.

    A sibling of :func:`metrics_dir` for the same reason :func:`driver_health_dir` is —
    ``duck.register_metrics`` globs ``metrics_dir()/**/*.parquet`` recursively with
    ``union_by_name=true``, so a differently-shaped dataset nested in that tree would
    silently corrupt that view. Note the partition key is ``snapshot_month``, not
    ``charge_month``: this is a point-in-time metadata inventory, not a charge period
    (see :mod:`flashlight.lake.storage_location_schema`).
    """
    return home() / "storage_locations"


def compute_instances_dir() -> Path:
    """Compute-instance telemetry root, Hive-partitioned
    ``provider_name=…/charge_month=…/``.

    A sibling of :func:`metrics_dir` for the same reason :func:`driver_health_dir` is —
    ``duck.register_metrics`` globs ``metrics_dir()/**/*.parquet`` recursively with
    ``union_by_name=true``, so a differently-shaped dataset nested in that tree would
    silently corrupt that view. Unlike :func:`storage_locations_dir`, the partition key
    is ``charge_month`` (a real charge period, not a snapshot): the source
    (``system.compute.node_timeline``) reports bounded historical activity, not
    present-tense state — see :mod:`flashlight.lake.compute_instance_schema`.
    """
    return home() / "compute_instances"


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
    onto the same volume the user already gave us space on — unless
    ``FLASHLIGHT_DUCKDB_TEMP_DIR`` names somewhere else, which is what a deployment with
    a read-only lake home needs (see that setting's own comment for why it isn't inferred).
    """
    override = get_settings().duckdb_temp_dir
    if override:
        return Path(override).expanduser()
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


def mcp_log_path() -> Path:
    """Output of an MCP server the dashboard launched (``dashboard/mcp_runner.py``).

    One rolling file, not one per run like :func:`sync_log_path`: a server has no run id
    and no natural end, and appending keeps a failed start readable next to the retry.
    """
    return meta_dir() / "mcp_server.log"


def assistant_turns_dir() -> Path:
    """BYOK assistant usage log — one Parquet file per assistant turn (append-only)."""
    return meta_dir() / "assistant_turns"


def legacy_assistant_turns_dir() -> Path:
    """Where the assistant usage log lived before the chat -> assistant rename.

    Read-only: :func:`flashlight.lake.duck.register_assistant_turns` still folds
    this directory in so an existing install's history doesn't vanish from
    ``/usage`` (the same read-the-old-name-too courtesy, for the same reason, as
    the legacy keychain service in
    :mod:`flashlight.dashboard.assistant_credentials`). Nothing ever writes here,
    so it decays as new turns land under the current name. Remove both once
    that's had a release or two to happen.
    """
    return meta_dir() / "chat_turns"


def ensure_layout() -> None:
    """Create the lake directory skeleton (idempotent)."""
    for path in (
        config_dir(),
        bronze_dir(),
        metrics_dir(),
        driver_health_dir(),
        ai_usage_dir(),
        storage_locations_dir(),
        compute_instances_dir(),
        gold_dir(),
        runs_dir(),
        sync_logs_dir(),
        assistant_turns_dir(),
    ):
        path.mkdir(parents=True, exist_ok=True)
