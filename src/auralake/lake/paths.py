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


def gold_dir() -> Path:
    """GOLD root — one ``<view>.parquet`` per catalogued metric (consumer surface)."""
    return home() / "gold"


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
    for path in (config_dir(), bronze_dir(), gold_dir(), runs_dir()):
        path.mkdir(parents=True, exist_ok=True)
