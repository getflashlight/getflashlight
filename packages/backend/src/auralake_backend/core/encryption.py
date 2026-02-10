"""Fernet encryption wrapper for credential storage."""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.environ.get("AURALAKE_ENCRYPTION_KEY", "")
        if not key:
            raise RuntimeError(
                "AURALAKE_ENCRYPTION_KEY environment variable is required "
                "for credential encryption. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a string and return the ciphertext as a URL-safe base64 string."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet token back to the original string."""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Failed to decrypt: invalid token or wrong key") from exc
