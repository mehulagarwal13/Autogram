"""Local-disk StorageBackend — today's exact pre-abstraction behavior."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.services.storage.base import StorageBackend


class LocalStorageBackend(StorageBackend):
    """Default backend. A `key` is a filesystem path (absolute, or relative
    to the process working directory — exactly what `file_storage.py` and
    `document_storage.py` passed to `open()` directly before this
    abstraction existed). Behavior is byte-for-byte identical to the
    pre-abstraction code: same directories, same `open(..., "wb")` write."""

    def save(self, key: str, content: bytes) -> str:
        path = Path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        return str(path)

    def read(self, key: str) -> bytes:
        return Path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return Path(key).exists()

    def delete(self, key: str) -> None:
        path = Path(key)
        if path.exists():
            path.unlink()

    @contextmanager
    def local_path(self, key: str) -> Iterator[str]:
        # No-op: the key already IS a real filesystem path. No copy, no I/O.
        yield str(Path(key))
