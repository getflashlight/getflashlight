"""On-disk layout for the Parquet lake — the single source of truth for *where*
persistent data lives. Everything hangs off :func:`home`, so the writer and the
readers resolve identical paths.
"""

from __future__ import annotations

from pathlib import Path

import platformdirs

from auralake.core.settings import get_settings


def home() -> Path:
    """Lake root: ``AURALAKE_HOME`` if set, else the platform user-data dir."""
    configured = get_settings().home
    if configured:
        return Path(configured).expanduser()
    return Path(platformdirs.user_data_dir("auralake"))


def config_dir() -> Path:
    return home() / "config"


def connections_path() -> Path:
    """Default connector config location (``auralake init`` writes here)."""
    return config_dir() / "connections.yml"


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


def gold_staging_dir() -> Path:
    """Transient dir a transform builds GOLD into before the atomic publish swap."""
    return home() / "gold.staging"


def meta_dir() -> Path:
    return home() / "meta"


def runs_dir() -> Path:
    """Ingest run log — one Parquet file per run (append-only, concurrency-safe)."""
    return meta_dir() / "runs"


def ensure_layout() -> None:
    """Create the lake directory skeleton (idempotent)."""
    for path in (config_dir(), bronze_dir(), metrics_dir(), gold_dir(), runs_dir()):
        path.mkdir(parents=True, exist_ok=True)
