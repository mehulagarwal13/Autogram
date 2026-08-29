"""
Session/pacing helpers — Phase 2 (see ARCHITECTURE.md).

`HumanPacing` encodes the non-negotiable throttle floors from
ARCHITECTURE.md ("Compliance & Risk"): per-character typing delay,
per-action delay, inter-application delay, daily application cap, and a
working-hours window. These floors must not be configurable below the
defaults below — only relaxed upward.

`SessionStore` persists Playwright storage-state (cookies/local-storage) per
(user_id, ats_platform), encrypted at rest, using the same Fernet key
management this application already uses for candidate profile PII
(`app.core.crypto`, `ENCRYPTION_KEY` in `.env`). `automation/` is now an
internal module of this application and is expected to import `app.core.*`
directly (see `automation/interfaces.py` and `automation/README.md`) — there
is no injected-dependency indirection here anymore.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from app.core.config import AUTOMATION_SESSION_DIR
from app.core.crypto import decrypt_field, encrypt_field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HumanPacing:
    per_char_delay_ms_min: int = 50
    per_char_delay_ms_max: int = 200
    per_action_delay_s_min: float = 2.0
    per_action_delay_s_max: float = 7.0
    inter_application_delay_s_min: float = 60.0
    inter_application_delay_s_max: float = 180.0
    daily_application_cap: int = 30
    working_hours_start: int = 10  # local hour, 24h clock
    working_hours_end: int = 20


DEFAULT_PACING = HumanPacing()


class SessionStore:
    """Encrypted per-(user, ats) Playwright storage-state persistence.

    Only the resulting cookies/local-storage from a session the user
    authenticated themselves are ever persisted here — never a third-party
    password (see ARCHITECTURE.md, "No password harvesting"). Encryption
    reuses `app.core.crypto.encrypt_field` / `decrypt_field` (Fernet, the
    same `ENCRYPTION_KEY` already required for candidate PII) rather than
    inventing a second encryption scheme for this project.
    """

    def __init__(self, base_dir: str | Path | None = None):
        self._base_dir = Path(base_dir) if base_dir is not None else Path(AUTOMATION_SESSION_DIR)
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, user_id: str, ats_platform: str) -> Path:
        # user_id/ats_platform are our own identifiers (UUIDs / a fixed enum
        # of platform names), never raw user input from a form — safe to use
        # directly in a filename after a defensive separator swap.
        safe_user = user_id.replace("/", "_").replace("\\", "_")
        safe_ats = ats_platform.replace("/", "_").replace("\\", "_")
        return self._base_dir / f"{safe_user}__{safe_ats}.session.enc"

    def load(self, user_id: str, ats_platform: str) -> dict | None:
        """Returns the decrypted Playwright `storage_state` dict, or `None`
        if there's no saved session (or it couldn't be decrypted/parsed —
        treated as "no session" rather than a hard failure, since the caller's
        fallback is simply a fresh, unauthenticated context)."""
        path = self._path_for(user_id, ats_platform)
        if not path.exists():
            return None

        ciphertext = path.read_text(encoding="utf-8")
        plaintext = decrypt_field(ciphertext)
        if plaintext is None:
            logger.warning(
                "Session file for user=%s ats=%s could not be decrypted (wrong/rotated "
                "ENCRYPTION_KEY?) — discarding and treating as no session.",
                user_id, ats_platform,
            )
            return None

        try:
            return json.loads(plaintext)
        except json.JSONDecodeError:
            logger.warning(
                "Session file for user=%s ats=%s was not valid JSON — discarding.",
                user_id, ats_platform,
            )
            return None

    def save(self, user_id: str, ats_platform: str, storage_state: dict) -> None:
        """Encrypts and writes `storage_state` (the dict returned by
        Playwright's `BrowserContext.storage_state()`), overwriting any
        previous session for this (user, ats) pair."""
        path = self._path_for(user_id, ats_platform)
        plaintext = json.dumps(storage_state)
        ciphertext = encrypt_field(plaintext)
        path.write_text(ciphertext, encoding="utf-8")

    def has_session(self, user_id: str, ats_platform: str) -> bool:
        return self._path_for(user_id, ats_platform).exists()

    def delete(self, user_id: str, ats_platform: str) -> None:
        """Removes a saved session — e.g. after the ATS rejects it (expired
        cookies) so the next run falls back to a fresh/manual-login context."""
        path = self._path_for(user_id, ats_platform)
        if path.exists():
            path.unlink()
