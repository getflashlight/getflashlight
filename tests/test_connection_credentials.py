from __future__ import annotations

from flashlight.dashboard import connection_credentials


def test_load_secret_returns_keychain_value(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(connection_credentials, "_keyring_get", lambda env_name: "shh")
    assert connection_credentials.load_secret("DATABRICKS_TOKEN") == "shh"


def test_load_secret_returns_none_when_unset(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(connection_credentials, "_keyring_get", lambda env_name: None)
    assert connection_credentials.load_secret("DATABRICKS_TOKEN") is None


def test_load_secret_survives_a_backend_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def _raises(env_name: str) -> str | None:
        raise RuntimeError("keychain locked")

    monkeypatch.setattr(connection_credentials, "_keyring_get", _raises)
    assert connection_credentials.load_secret("DATABRICKS_TOKEN") is None


def test_save_secret_returns_true_on_success(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    saved: dict[str, str] = {}
    monkeypatch.setattr(connection_credentials, "_keyring_set", saved.__setitem__)

    assert connection_credentials.save_secret("DATABRICKS_TOKEN", "sk-new") is True
    assert saved == {"DATABRICKS_TOKEN": "sk-new"}


def test_save_secret_returns_false_on_backend_failure(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def _raises(env_name: str, value: str) -> None:
        raise RuntimeError("no keyring backend")

    monkeypatch.setattr(connection_credentials, "_keyring_set", _raises)
    assert connection_credentials.save_secret("DATABRICKS_TOKEN", "sk-new") is False


def test_load_and_save_are_scoped_per_env_name(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    store: dict[str, str] = {}
    monkeypatch.setattr(connection_credentials, "_keyring_get", store.get)
    monkeypatch.setattr(connection_credentials, "_keyring_set", store.__setitem__)

    connection_credentials.save_secret("DATABRICKS_TOKEN", "sk-databricks")
    connection_credentials.save_secret("AWS_SECRET_ACCESS_KEY", "sk-aws")

    assert connection_credentials.load_secret("DATABRICKS_TOKEN") == "sk-databricks"
    assert connection_credentials.load_secret("AWS_SECRET_ACCESS_KEY") == "sk-aws"
