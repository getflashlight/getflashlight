"""BYOK assistant model choice, persisted to ``<home>/config/assistant.yml``.

Which model answers questions is *configuration*, so it belongs beside
``connections.yml`` and ``policies.yml`` in the one directory a user backs up or
mounts — not in NiceGUI's ``app.storage.general``, where it used to live. That store
lands wherever ``NICEGUI_STORAGE_PATH`` points, and pointing it at a tmpfs is the
supported way to run with a read-only lake home (the shipped image does exactly that,
``Dockerfile``) — so a setting kept there is forgotten on restart in precisely the
deployment that can least afford to re-prompt for it. It was also invisible to the CLI
and MCP: nothing outside the dashboard process could tell which model was configured.

Precedence on load, widest override first::

    FLASHLIGHT_ASSISTANT_PROVIDER / _MODEL / _BASE_URL   (env — a container can be
                                                          configured with no file
                                                          and no click-through)
    <home>/config/assistant.yml                           (what the gear dialog writes)
    the UI's own preset defaults                          (views/assistant.py::_PRESETS)

**No secret is stored here.** The API key stays in the OS keychain with an
env-var fallback (:mod:`flashlight.dashboard.assistant_credentials`), so
``assistant.yml`` is safe to commit, copy between machines, or mount read-only.

``provider`` is the internal id the engine dispatches on (``openai`` /
``anthropic`` / ``google`` / ``openai_compatible``) and is the only load-bearing
field. ``preset`` is the dialog's dropdown label, stored purely so reopening the
dialog lands on the row the user picked — several presets share one provider id
(Ollama, Databricks and Custom are all ``openai_compatible``), so the id alone
can't restore it. Nothing functional keys off ``preset``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import yaml
from pydantic import BaseModel, Field

from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.lake import paths

logger = get_logger(__name__)


class AssistantConfig(BaseModel):
    """Everything needed to reach a model, minus the credential."""

    provider: str | None = Field(
        default=None,
        description="Engine dispatch id: openai | anthropic | google | openai_compatible.",
    )
    model: str | None = Field(
        default=None,
        description="Model name as the provider's API expects it, e.g. claude-sonnet-4-5.",
    )
    base_url: str | None = Field(
        default=None,
        description="Endpoint override — a Databricks serving URL, Ollama, or any "
        "self-hosted OpenAI-compatible API. Leave unset for the native providers.",
    )
    preset: str | None = Field(
        default=None,
        description="Which row of the settings dialog's provider list was chosen. "
        "Cosmetic: restores the dropdown, never used as a lookup key.",
    )


def env_overrides() -> dict[str, str]:
    """Field → value for each assistant field pinned by an env var.

    The dialog names these rather than silently ignoring an edit: with
    ``FLASHLIGHT_ASSISTANT_MODEL`` set, saving a different model in the UI writes the
    file but the env value still answers the next question, which reads as the setting
    not sticking.
    """
    settings = get_settings()
    env = {
        "provider": settings.assistant_provider,
        "model": settings.assistant_model,
        "base_url": settings.assistant_base_url,
    }
    return {k: v for k, v in env.items() if v}


def _read_yaml() -> dict[str, Any]:
    path = paths.assistant_config_path()
    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a mapping, got {type(raw).__name__}")
    # Accept both a bare mapping and one nested under `assistant:` so the scaffolded
    # file can carry a heading without the loader caring which shape it gets — same
    # courtesy as efficiency/policy_config.py's reader. `assistant:` with every field
    # still commented out parses as None, which is the scaffolded file untouched: not
    # malformed, just nothing configured yet.
    if "assistant" in raw:
        nested = raw["assistant"]
        return nested if isinstance(nested, dict) else {}
    return raw


@lru_cache
def load() -> AssistantConfig:
    """Cached config: env over ``config/assistant.yml`` over "nothing configured".

    A missing file means "not configured yet", not an error — the dialog opens and
    the user picks. A malformed one is loud: silently falling back to defaults would
    quietly answer questions with a different model than the file names.

    Cached because the dialog reads it on every page load; call
    :func:`load.cache_clear` after a write (see :func:`save`).
    """
    path = paths.assistant_config_path()
    cfg = AssistantConfig.model_validate(_read_yaml()) if path.exists() else AssistantConfig()
    overrides = env_overrides()
    if overrides:
        cfg = cfg.model_copy(update=overrides)
        logger.info("assistant_config_env_override", fields=sorted(overrides))
    return cfg


def save(cfg: AssistantConfig) -> None:
    """Write *cfg* to ``config/assistant.yml`` and drop the cache.

    A plain full-file rewrite, matching ``ingest/config.py``'s ``save_connections``
    — this file has no comments to preserve once the app owns it, and the scaffolded
    template is only there for the hand-editing case.
    """
    path = paths.assistant_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"assistant": cfg.model_dump(exclude_none=True)}
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    load.cache_clear()
    logger.info("assistant_config_written", path=str(path))
