from __future__ import annotations

from flashlight.dashboard import assistant_credentials


def test_load_api_key_prefers_keychain_over_env_var(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(assistant_credentials, "_keyring_get", lambda provider: "sk-keychain")
    monkeypatch.setenv(assistant_credentials.ENV_VAR, "sk-env")

    assert assistant_credentials.load_api_key("OpenAI") == "sk-keychain"


def test_load_api_key_falls_back_to_env_var_when_keychain_empty(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(assistant_credentials, "_keyring_get", lambda provider: None)
    monkeypatch.setenv(assistant_credentials.ENV_VAR, "sk-env")

    assert assistant_credentials.load_api_key("OpenAI") == "sk-env"


def test_load_api_key_returns_none_when_nothing_is_set(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(assistant_credentials, "_keyring_get", lambda provider: None)
    monkeypatch.delenv(assistant_credentials.ENV_VAR, raising=False)

    assert assistant_credentials.load_api_key("OpenAI") is None


def test_load_api_key_survives_a_backend_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A locked/unavailable OS keychain must degrade to the env var, not raise."""

    def _raises(provider: str) -> str | None:
        raise RuntimeError("keychain locked")

    monkeypatch.setattr(assistant_credentials, "_keyring_get", _raises)
    monkeypatch.setenv(assistant_credentials.ENV_VAR, "sk-env")

    assert assistant_credentials.load_api_key("OpenAI") == "sk-env"


def test_save_api_key_returns_true_on_success(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    saved: dict[str, str] = {}
    monkeypatch.setattr(assistant_credentials, "_keyring_set", saved.__setitem__)

    assert assistant_credentials.save_api_key("OpenAI", "sk-new") is True
    assert saved == {"OpenAI": "sk-new"}


def test_save_api_key_returns_false_on_backend_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def _raises(provider: str, value: str) -> None:
        raise RuntimeError("no keyring backend")

    monkeypatch.setattr(assistant_credentials, "_keyring_set", _raises)

    assert assistant_credentials.save_api_key("OpenAI", "sk-new") is False


def test_load_and_save_are_scoped_per_provider(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store: dict[str, str] = {}
    monkeypatch.setattr(assistant_credentials, "_keyring_get", store.get)
    monkeypatch.setattr(assistant_credentials, "_keyring_set", store.__setitem__)

    assistant_credentials.save_api_key("OpenAI", "sk-openai")
    assistant_credentials.save_api_key("Anthropic (Claude)", "sk-anthropic")

    assert assistant_credentials.load_api_key("OpenAI") == "sk-openai"
    assert assistant_credentials.load_api_key("Anthropic (Claude)") == "sk-anthropic"
