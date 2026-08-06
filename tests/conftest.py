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
