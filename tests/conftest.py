"""Test-process setup shared by every test module.

Redirects NiceGUI's on-disk ``app.storage.general``/``app.storage.user`` files
away from the repo before ``nicegui`` is ever imported — its default location
(``./.nicegui``) is read once, at class-definition time, so this has to run
before any test module (transitively) imports ``nicegui``, which conftest.py
loading always precedes.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator

import pytest

os.environ.setdefault("NICEGUI_STORAGE_PATH", tempfile.mkdtemp(prefix="flashlight-test-nicegui-"))


@pytest.fixture(autouse=True)
def _no_real_keyring(monkeypatch):  # type: ignore[no-untyped-def]
    """Never let a test touch the real OS keychain — it can hang waiting on a
    permission prompt outside an interactive desktop session, and would leave
    real entries behind. Defaults to "nothing stored"; a test that needs to
    exercise persistence overrides these with its own in-memory fake."""
    from flashlight.dashboard import assistant_credentials
    from flashlight.ingest import connection_credentials

    monkeypatch.setattr(assistant_credentials, "_keyring_get", lambda provider: None)
    monkeypatch.setattr(assistant_credentials, "_keyring_set", lambda provider, value: None)
    monkeypatch.setattr(connection_credentials, "_keyring_get", lambda env_name: None)
    monkeypatch.setattr(connection_credentials, "_keyring_set", lambda env_name, value: None)


@pytest.fixture(autouse=True)
def _fresh_policy_thresholds():  # type: ignore[no-untyped-def]
    """Policy thresholds are cached per process and resolved from FLASHLIGHT_HOME, so a
    test that writes its own ``policies.yml`` would otherwise leak those values into
    every later test (and inherit an earlier test's)."""
    from flashlight.efficiency.policy_config import get_thresholds

    get_thresholds.cache_clear()
    yield
    get_thresholds.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_assistant_config():  # type: ignore[no-untyped-def]
    """Same reason as the thresholds above: ``config/assistant.yml`` is cached per
    process and resolved from FLASHLIGHT_HOME."""
    from flashlight.dashboard.assistant_config import load

    load.cache_clear()
    yield
    load.cache_clear()


@pytest.fixture(autouse=True)
def _reset_nicegui_clients() -> Iterator[None]:
    """Keep page simulations isolated across tests.

    NiceGUI's application reset clears configuration and storage but deliberately
    leaves live clients alone. A client created by ``user_simulation`` can therefore
    leave its page elements visible to the next test's finder, making a later route
    assertion inspect the previous test's page. Delete those clients before resetting
    the app so every simulation starts from an empty page registry and client set.
    """
    from nicegui import app
    from nicegui.client import Client

    def reset() -> None:
        for client in list(Client.instances.values()):
            client.delete()
        app.reset()

    reset()
    yield
    reset()
