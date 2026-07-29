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
    from flashlight.dashboard import chat_credentials

    monkeypatch.setattr(chat_credentials, "_keyring_get", lambda provider: None)
    monkeypatch.setattr(chat_credentials, "_keyring_set", lambda provider, value: None)
