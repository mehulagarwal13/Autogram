"""
Storage backend abstraction — the single seam between "where do file bytes
live" and every module that reads/writes them.

Why this exists: `app/services/file_storage.py` and
`app/services/document_storage.py` previously called `open()`/`Path` directly
against a local directory. That's fine for a single instance but breaks the
moment Autogram runs on more than one API instance (local disk isn't shared
across instances) or needs S3-grade durability for user documents. See
PHASE2_ARCHITECTURE.md Initiative 4 for the full rationale.

Design: two implementations behind one abstract base class —
  - `LocalStorageBackend` — today's exact behavior, zero regression risk.
    Default (`STORAGE_BACKEND=local` or unset).
  - `S3StorageBackend` — writes to any S3-compatible object store (AWS S3,
    Cloudflare R2, MinIO). Selected via `STORAGE_BACKEND=s3`.

Known boundary (flagged deliberately, not hidden): `local_path()` gives a
caller a real filesystem path for a stored object regardless of backend (a
no-op for Local, a temp-file download for S3) — this is what a caller that
needs a real path on disk should use. Two existing call sites still read
`record.stored_path` directly instead of going through this: pdfplumber/
python-docx in `app/services/text_extraction.py`, and Playwright's file input
in `automation/ats/base.py::upload_resume()`. Both need a real local path and
are NOT yet routed through `local_path()` — that rewiring is the explicit
next step before `STORAGE_BACKEND=s3` is safe to run in an environment that
also does text extraction or browser automation. Tracked as follow-up in
PHASE2_ARCHITECTURE.md Initiative 4. Save/delete are fully correct under S3
today; those two specific reads are the gap.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ContextManager


class StorageBackend(ABC):
    """A `key` is a backend-relative identifier. For `LocalStorageBackend` a
    key IS the filesystem path (unchanged from pre-abstraction behavior). For
    `S3StorageBackend` a key is the S3 object key (or the `s3://bucket/key`
    locator that `save()` hands back and that gets persisted as
    `stored_path` in the DB — both forms are accepted on read/delete)."""

    @abstractmethod
    def save(self, key: str, content: bytes) -> str:
        """Writes `content` under `key`. Returns the locator to persist as
        `stored_path` — may differ from `key` (e.g. an `s3://` URI)."""
        raise NotImplementedError

    @abstractmethod
    def read(self, key: str) -> bytes:
        """Reads the full contents addressed by `key`."""
        raise NotImplementedError

    @abstractmethod
    def exists(self, key: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    def delete(self, key: str) -> None:
        """Best-effort: deleting a missing key is not an error, matching the
        pre-abstraction `document_storage.delete_document_file` contract."""
        raise NotImplementedError

    @abstractmethod
    def local_path(self, key: str) -> ContextManager[str]:
        """Context manager yielding a real filesystem path to `key`'s
        contents. Local: the path itself (no I/O). S3: downloads to a temp
        file, removed on exit. Use this — not `read()` — when the consumer
        needs an actual path (Playwright uploads, pdfplumber/python-docx)."""
        raise NotImplementedError
