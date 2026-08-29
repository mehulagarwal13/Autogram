"""
Field-level encryption for sensitive candidate PII (phone, address).

Uses Fernet (AES-128-CBC + HMAC, `cryptography` package) with a symmetric key
from `ENCRYPTION_KEY` in `.env` (fail-fast in `app/core/config.py` if unset,
matching the existing JWT_SECRET/OPENAI_API_KEY pattern in this codebase).

Only columns that never need server-side filtering/searching are encrypted
this way (phone, address). `email` stays plaintext because it's the login
identifier and must remain indexable/unique — same tradeoff already made
elsewhere in this codebase.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import ENCRYPTION_KEY

_fernet = Fernet(ENCRYPTION_KEY.encode("utf-8") if isinstance(ENCRYPTION_KEY, str) else ENCRYPTION_KEY)


def encrypt_field(value: str | None) -> str | None:
    """Encrypts a plaintext string for storage. Returns None unchanged so
    optional profile fields (e.g. no address given) stay NULL, not an
    encrypted-empty-string."""
    if value is None:
        return None
    return _fernet.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_field(value: str | None) -> str | None:
    """Decrypts a stored value back to plaintext for the owning user's own
    API responses. Never raises on a corrupt/foreign token — returns None so
    a bad value can't crash a profile read."""
    if value is None:
        return None
    try:
        return _fernet.decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None
