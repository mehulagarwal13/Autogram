"""
Storage for master-profile documents: resume versions, cover letters,
certificates, and other supporting files (§1 "Documents" / §7 "Resume Upload
Automation" of the project brief).

Generalizes `app/services/file_storage.py` (which stays as-is, dedicated to
the existing single-resume matching pipeline) to support multiple document
types, each with its own allowed extensions and magic-byte validation —
same "don't trust the filename" principle.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from app.services.storage import get_storage_backend

# Deliberate RE-EXPORT, not an unused import: `app/api/profile.py` imports
# `compute_file_hash` from this module (alongside the save/delete helpers) so
# callers have one document-storage entry point rather than reaching past it
# into `file_storage`. `__all__` states that intent, and stops a linter's
# unused-import autofix from silently deleting it again — which it did once,
# turning every profile-document upload into an ImportError.
from app.services.file_storage import compute_file_hash

__all__ = [
    "ALLOWED_EXTENSIONS_BY_TYPE",
    "MAX_FILE_SIZE_MB",
    "compute_file_hash",
    "delete_document_file",
    "save_document_file",
    "stage_stored_file_for_agent",
    "save_task_upload_file",
]

STORAGE_DIR = Path("storage/documents")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)  # local-mode default; ignored under STORAGE_BACKEND=s3

# Files supplied while an autonomous task is paused have a deliberately
# different lifecycle from the user's permanent document library.  Playwright
# needs a real local path (not an s3:// locator), and the autonomous runner is
# currently in-process, so these are staged locally and namespaced by task.
# The random stored name also means an untrusted browser filename is never used
# as a path component.
TASK_UPLOAD_DIR = Path("storage/task_uploads")
AGENT_UPLOAD_CACHE_DIR = TASK_UPLOAD_DIR / "stored_document_cache"

ALLOWED_EXTENSIONS_BY_TYPE: dict[str, set[str]] = {
    "resume": {".pdf", ".docx"},
    "cover_letter": {".pdf", ".docx"},
    "certificate": {".pdf", ".png", ".jpg", ".jpeg"},
    "other": {".pdf", ".docx", ".png", ".jpg", ".jpeg", ".txt"},
}

MAGIC_BYTES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    ".docx": (b"PK\x03\x04",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".txt": (),  # plain text has no reliable magic bytes — extension check only
}

MAX_FILE_SIZE_MB = 10


def validate_extension(document_type: str, filename: str) -> str:
    if document_type not in ALLOWED_EXTENSIONS_BY_TYPE:
        raise ValueError(f"Unknown document type: {document_type}.")

    ext = Path(filename).suffix.lower()
    allowed = ALLOWED_EXTENSIONS_BY_TYPE[document_type]
    if ext not in allowed:
        raise ValueError(
            f"Unsupported file type '{ext}' for document type '{document_type}'. "
            f"Allowed: {', '.join(sorted(allowed))}."
        )
    return ext


def validate_content(ext: str, content: bytes) -> None:
    """Rejects files whose bytes don't match their claimed type (e.g. renamed .exe)."""
    signatures = MAGIC_BYTES.get(ext, ())
    if signatures and not any(content.startswith(sig) for sig in signatures):
        raise ValueError(
            f"File content does not match a valid {ext} file. "
            "The file may be corrupt or renamed from another format."
        )


def save_document_file(document_type: str, filename: str, content: bytes) -> tuple[str, str]:
    """
    Validates extension AND content signature, then saves via the active
    StorageBackend (local disk by default; S3-compatible when
    STORAGE_BACKEND=s3 — see app/services/storage/), namespaced by document
    type. Returns (document_id, stored_path).
    """
    ext = validate_extension(document_type, filename)
    validate_content(ext, content)

    type_dir = STORAGE_DIR / document_type
    document_id = str(uuid.uuid4())
    key = str(type_dir / f"{document_id}{ext}")

    stored_path = get_storage_backend().save(key, content)

    return document_id, stored_path


def save_task_upload_file(
    task_id: str, document_type: str, filename: str, content: bytes
) -> tuple[str, str]:
    """Validate and stage a document for one live autonomous task.

    Unlike ``save_document_file``, this always produces a local path because
    Playwright's file-input API cannot consume an object-store URI.  The path
    is still safe to hand to the model-driven executor: it is added to that
    task's explicit upload allowlist, and no other local path is accepted.
    """
    ext = validate_extension(document_type, filename)
    validate_content(ext, content)

    document_id = str(uuid.uuid4())
    task_dir = TASK_UPLOAD_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / f"{document_id}{ext}"
    path.write_bytes(content)
    return document_id, str(path.resolve())


def stage_stored_file_for_agent(
    cache_key: str, document_type: str, filename: str, stored_path: str
) -> str:
    """Materialize a stored document as a stable local Playwright upload.

    Object-storage locators cannot be supplied directly to a browser file
    input. Download through the configured storage backend and keep a local
    runner cache; the executor still accepts only the exact path allowlisted
    on the current task.
    """
    ext = validate_extension(document_type, filename)
    content = get_storage_backend().read(stored_path)
    validate_content(ext, content)

    AGENT_UPLOAD_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = AGENT_UPLOAD_CACHE_DIR / f"{cache_key}{ext}"
    path.write_bytes(content)
    return str(path.resolve())


def delete_document_file(stored_path: str) -> None:
    """Best-effort delete via the active StorageBackend — a missing file is
    not an error (already cleaned up)."""
    get_storage_backend().delete(stored_path)
