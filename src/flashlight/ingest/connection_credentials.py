"""Ingest connector secrets — OS keychain, same pattern as
:mod:`flashlight.dashboard.chat_credentials`.

Connector configs (``ingest/config.py``) only ever hold an env var *name*
(``token_env``, ``access_key_env``, ...); the actual secret value entered in the
dashboard's Connections page is stored here, keyed by that env var name, rather
than ever being written to ``connections.yml``. ``config.py``'s ``env()`` — the
one place every connector resolves a ``*_env`` name — checks the real process
environment first and falls back to :func:`load_secret` here, so a connector
config resolves the same secret the same way whether it's built by the
dashboard's subprocess or a bare ``flashlight ingest`` run in a terminal; there
is no separate "pre-populate the subprocess env" step to keep in sync with
this. Since the lookup key *is* the env var name, two connections that share
one would share one keychain entry — ``config.py``'s ``scoped_env_name``
defaults every connection's env var name to something derived from its own
(enforced-unique) ``name``, so this only happens when a user explicitly opts
into sharing one by hand-setting the same name twice.

Its own keychain service name (``flashlight-ingest``, vs. chat's
``flashlight-chat``) so a connector token and a chat API key never collide even
if they happened to share a lookup key.
"""

from __future__ import annotations

import keyring

from flashlight.core.logging import get_logger

logger = get_logger(__name__)

_SERVICE = "flashlight-ingest"


def _keyring_get(env_name: str) -> str | None:
    """Thin wrapper so tests can monkeypatch this instead of touching the real
    OS keychain (which would hang or prompt in a headless/CI environment)."""
    return keyring.get_password(_SERVICE, env_name)


def _keyring_set(env_name: str, value: str) -> None:
    keyring.set_password(_SERVICE, env_name, value)


def load_secret(env_name: str) -> str | None:
    """Best-effort keychain lookup for a connector secret, by its ``*_env`` name."""
    try:
        return _keyring_get(env_name)
    except Exception as exc:  # noqa: BLE001 - any backend failure is non-fatal, not exceptional
        logger.warning("keyring_read_failed", env_name=env_name, error=str(exc))
        return None


def save_secret(env_name: str, value: str) -> bool:
    """Best-effort save to the OS keychain. Returns whether it succeeded — the
    caller decides how to tell the user their secret won't persist otherwise."""
    try:
        _keyring_set(env_name, value)
    except Exception as exc:  # noqa: BLE001 - any backend failure is non-fatal, not exceptional
        logger.warning("keyring_write_failed", env_name=env_name, error=str(exc))
        return False
    return True
