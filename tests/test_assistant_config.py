"""``config/assistant.yml`` — the BYOK model choice, persisted beside connections.yml."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import yaml

from flashlight.core.settings import get_settings
from flashlight.dashboard import assistant_config
from flashlight.lake import paths


@pytest.fixture(autouse=True)
def lake_home(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    assistant_config.load.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    assistant_config.load.cache_clear()


def test_missing_file_means_nothing_configured_not_an_error() -> None:
    """First run: no file, no crash — the dialog opens and the user picks."""
    assert not paths.assistant_config_path().exists()
    cfg = assistant_config.load()
    assert cfg.provider is None
    assert cfg.model is None


def test_round_trips_through_the_config_dir() -> None:
    assistant_config.save(
        assistant_config.AssistantConfig(
            provider="anthropic", model="claude-sonnet-4-5", preset="Anthropic (Claude)"
        )
    )
    path = paths.assistant_config_path()
    assert path.parent == paths.config_dir()  # beside connections.yml / policies.yml
    assert assistant_config.load().model == "claude-sonnet-4-5"
    # Nested under an `assistant:` heading, and unset fields are omitted rather than
    # written as nulls the next reader has to interpret.
    written = yaml.safe_load(path.read_text())
    assert "base_url" not in written["assistant"]


def test_save_drops_the_cache_so_the_next_read_sees_the_write() -> None:
    """load() is cached (the dialog reads it on every page load), so a stale cache
    would show the previous model until the process restarted."""
    assistant_config.save(assistant_config.AssistantConfig(provider="openai", model="gpt-4o"))
    assert assistant_config.load().model == "gpt-4o"
    assistant_config.save(assistant_config.AssistantConfig(provider="openai", model="gpt-5"))
    assert assistant_config.load().model == "gpt-5"


def test_env_wins_over_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """A container is configured by env alone — no file, no click-through — and an
    env var must beat a file baked into the image."""
    assistant_config.save(
        assistant_config.AssistantConfig(provider="openai", model="gpt-4o", preset="OpenAI")
    )
    monkeypatch.setenv("FLASHLIGHT_ASSISTANT_PROVIDER", "anthropic")
    monkeypatch.setenv("FLASHLIGHT_ASSISTANT_MODEL", "claude-sonnet-4-5")
    get_settings.cache_clear()
    assistant_config.load.cache_clear()

    cfg = assistant_config.load()
    assert (cfg.provider, cfg.model) == ("anthropic", "claude-sonnet-4-5")
    assert assistant_config.env_overrides() == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
    }


def test_env_only_needs_no_file_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLASHLIGHT_ASSISTANT_MODEL", "gemini-2.0-flash")
    get_settings.cache_clear()
    assistant_config.load.cache_clear()
    assert not paths.assistant_config_path().exists()
    assert assistant_config.load().model == "gemini-2.0-flash"


def test_a_malformed_file_is_loud() -> None:
    """Silently falling back to defaults would answer questions with a different
    model than the file names."""
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.assistant_config_path().write_text("- not: a mapping\n")
    with pytest.raises(ValueError, match="must contain a mapping"):
        assistant_config.load()


def test_the_scaffolded_template_parses_as_nothing_configured() -> None:
    """`flashlight init` writes an all-commented template: `assistant:` with a None
    body. That's an untouched file, not a malformed one."""
    from flashlight import scaffold

    scaffold.scaffold()
    assert paths.assistant_config_path().exists()
    assert assistant_config.load().provider is None


def test_no_secret_field_exists_on_the_model() -> None:
    """The API key belongs in the OS keychain / FLASHLIGHT_ASSISTANT_API_KEY. This file
    is meant to be safe to commit and mount read-only, so it must never grow a place
    to put one."""
    fields = set(assistant_config.AssistantConfig.model_fields)
    assert not {f for f in fields if "key" in f or "secret" in f or "token" in f}
