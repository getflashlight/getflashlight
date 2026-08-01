"""Ingest connector secrets — OS keychain, same pattern as :mod:`chat_credentials`.

Connector configs (``ingest/config.py``) only ever hold an env var *name*
(``token_env``, ``access_key_env``, ...); the actual secret value entered in the
dashboard's Connections page is stored here, keyed by that env var name, and
resolved back into the ``flashlight ingest`` subprocess's environment at sync
time (see ``dashboard/ingest_runner.py``) rather than ever being written to
``connections.yml``.

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
