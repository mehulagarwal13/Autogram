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

from app.services.file_storage import compute_file_hash  # re-exported for callers

STORAGE_DIR = Path("storage/documents")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

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
    Validates extension AND content signature, then saves to disk under a
    unique ID, namespaced by document type. Returns (document_id, stored_path).
    """
    ext = validate_extension(document_type, filename)
    validate_content(ext, content)

    type_dir = STORAGE_DIR / document_type
    type_dir.mkdir(parents=True, exist_ok=True)

    document_id = str(uuid.uuid4())
    stored_path = type_dir / f"{document_id}{ext}"

    with open(stored_path, "wb") as f:
        f.write(content)

    return document_id, str(stored_path)


def delete_document_file(stored_path: str) -> None:
    """Best-effort delete — a missing file is not an error (already cleaned up)."""
    path = Path(stored_path)
    if path.exists():
        path.unlink()
