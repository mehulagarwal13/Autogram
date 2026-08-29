"""
Factory for the active `StorageBackend` — selected once via `STORAGE_BACKEND`
(`app.core.config`) and cached as a module-level singleton so
`file_storage.py` / `document_storage.py` don't each construct their own S3
client. See `base.py` for the abstraction's design and known boundaries.
"""

from __future__ import annotations

from app.services.storage.base import StorageBackend
from app.services.storage.local_backend import LocalStorageBackend

_backend: StorageBackend | None = None


def get_storage_backend() -> StorageBackend:
    global _backend
    if _backend is not None:
        return _backend

    from app.core import config

    if config.STORAGE_BACKEND == "s3":
        from app.services.storage.s3_backend import S3StorageBackend

        _backend = S3StorageBackend(
            bucket=config.S3_BUCKET,
            region=config.S3_REGION,
            endpoint_url=config.S3_ENDPOINT_URL,
        )
    else:
        _backend = LocalStorageBackend()

    return _backend


def reset_storage_backend() -> None:
    """Test-only: clears the cached singleton so tests can swap backends
    (and so `STORAGE_BACKEND` env changes take effect within a test run)."""
    global _backend
    _backend = None


__all__ = ["StorageBackend", "get_storage_backend", "reset_storage_backend"]
