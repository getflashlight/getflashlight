"""BYOK API key persistence — layered fallback: OS keychain, then an env var.

Standard practice for a local desktop/CLI app that needs to store a secret and
retrieve it later without re-prompting the user: the OS-native credential
store (macOS Keychain / Windows Credential Manager / Linux Secret Service),
via the ``keyring`` package. The decryption key is protected by the OS login
session and never touches Flashlight's own files — unlike an app-managed
"encrypted at rest" file, where the decryption key would have to live
somewhere the app itself can read, which gives any local attacker with the
same file access equivalent access (obfuscation, not real protection).

Falls back to an environment variable — the same ``*_env`` indirection
``ingest/config.py`` already uses for connector credentials — when no OS
keychain is reachable (e.g. a headless Linux server with no secret-service
daemon running). Never falls back to writing the key to a plain file: if
neither layer has it, the caller (``views/assistant.py``) just leaves the field
for the user to type each session.
"""

from __future__ import annotations

import os

import keyring

from flashlight.core.logging import get_logger

logger = get_logger(__name__)

_SERVICE = "flashlight-assistant"
ENV_VAR = "FLASHLIGHT_ASSISTANT_API_KEY"

# The page, module, keychain service and env var were all called "chat" before. Reads
# still fall through to the old names so an existing install doesn't silently lose a
# saved key and force the user to re-enter it; writes only ever use the new ones, so
# the legacy entry decays on the next save. Remove both once that's had a release or
# two to happen.
_LEGACY_SERVICE = "flashlight-chat"
_LEGACY_ENV_VAR = "FLASHLIGHT_CHAT_API_KEY"


def _keyring_get(provider: str) -> str | None:
    """Thin wrapper so tests can monkeypatch this instead of touching the real
    OS keychain (which would hang or prompt in a headless/CI environment)."""
    return keyring.get_password(_SERVICE, provider) or keyring.get_password(
        _LEGACY_SERVICE, provider
    )


def _keyring_set(provider: str, value: str) -> None:
    keyring.set_password(_SERVICE, provider, value)


def load_api_key(provider: str) -> str | None:
    """Best-effort lookup: OS keychain first, then the env var fallback."""
    try:
        stored = _keyring_get(provider)
    except Exception as exc:  # noqa: BLE001 - any backend failure is non-fatal, not exceptional
        logger.warning("keyring_read_failed", provider=provider, error=str(exc))
        stored = None
    return stored or os.environ.get(ENV_VAR) or os.environ.get(_LEGACY_ENV_VAR) or None


def save_api_key(provider: str, value: str) -> bool:
    """Best-effort save to the OS keychain. Returns whether it succeeded — the
    caller decides how to tell the user their key won't persist otherwise."""
    try:
        _keyring_set(provider, value)
    except Exception as exc:  # noqa: BLE001 - any backend failure is non-fatal, not exceptional
        logger.warning("keyring_write_failed", provider=provider, error=str(exc))
        return False
    return True
