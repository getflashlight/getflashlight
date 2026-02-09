"""YAML configuration loading for auralake.

Resolution order for the configuration file path:

1. Explicit *path* argument passed to :func:`load_config`.
2. The ``AURALAKE_CONFIG`` environment variable.
3. ``auralake.yaml`` in the current working directory.
4. ``~/.auralake/config.yaml`` as a user-level fallback.

Selected fields can be overridden through environment variables:

* ``AURALAKE_DATABASE_URL`` -- overrides ``database.url``
* ``AURALAKE_PROVIDER``     -- overrides ``provider``
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from auralake.core.exceptions import ConfigError
from auralake.models.config import AuraLakeConfig

_DEFAULT_FILENAMES: list[Path] = [
    Path("auralake.yaml"),
    Path.home() / ".auralake" / "config.yaml",
]


def _resolve_config_path(path: Path | None = None) -> Path:
    """Return the first configuration file that exists.

    Raises :class:`ConfigError` when no file can be found.
    """
    candidates: list[Path] = []

    if path is not None:
        candidates.append(Path(path))

    env_path = os.environ.get("AURALAKE_CONFIG")
    if env_path:
        candidates.append(Path(env_path))

    candidates.extend(_DEFAULT_FILENAMES)

    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file():
            return resolved

    searched = ", ".join(str(c) for c in candidates)
    raise ConfigError(f"No configuration file found. Searched: {searched}")


def _apply_env_overrides(data: dict) -> dict:  # type: ignore[type-arg]
    """Overlay environment-variable overrides onto the raw config dict."""
    db_url = os.environ.get("AURALAKE_DATABASE_URL")
    if db_url:
        data.setdefault("database", {})["url"] = db_url

    provider = os.environ.get("AURALAKE_PROVIDER")
    if provider:
        data["provider"] = provider

    return data


def load_config(path: Path | None = None) -> AuraLakeConfig:
    """Load, validate, and return the auralake configuration.

    Parameters
    ----------
    path:
        Optional explicit path to a YAML configuration file.  When *None*
        the standard resolution order is used (see module docstring).

    Returns
    -------
    AuraLakeConfig
        A fully validated Pydantic model instance.

    Raises
    ------
    ConfigError
        If the file cannot be found, read, or fails validation.
    """
    config_path = _resolve_config_path(path)

    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read config file {config_path}: {exc}") from exc

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"Expected a YAML mapping in {config_path}, got {type(data).__name__}")

    data = _apply_env_overrides(data)

    try:
        return AuraLakeConfig.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"Configuration validation failed: {exc}") from exc
