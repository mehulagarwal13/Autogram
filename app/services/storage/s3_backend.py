"""
S3-compatible StorageBackend — AWS S3, Cloudflare R2, or MinIO, selected via
`STORAGE_BACKEND=s3` (see `app.core.config`).

Read-path caveat (see `base.py` module docstring for the full explanation):
`text_extraction.py` and `automation/ats/base.py::upload_resume()` still read
`stored_path` as a raw local path today. Save/delete are fully correct under
S3; those two specific reads are a tracked follow-up, not silently broken —
flagging it here again because it's the detail most likely to bite someone
who flips `STORAGE_BACKEND=s3` without reading PHASE2_ARCHITECTURE.md first.
"""

from __future__ import annotations

import logging
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.services.storage.base import StorageBackend

logger = logging.getLogger(__name__)


class S3StorageBackend(StorageBackend):
    """A `key` is the S3 object key (e.g. "resumes/<uuid>.pdf" — forward
    slashes, no leading slash). `save()` also accepts the `s3://bucket/key`
    locator it itself returns (and that gets persisted as `stored_path`) on
    subsequent read/exists/delete/local_path calls — both forms work.

    Requires `boto3` (see requirements.txt), imported lazily below so it's
    only a hard dependency when this backend is actually selected.
    """

    def __init__(self, *, bucket: str, region: str | None = None, endpoint_url: str | None = None):
        # Local import: only required when STORAGE_BACKEND=s3, so the
        # default local-disk deployment does not need boto3 installed.
        import boto3  # noqa: PLC0415

        self._bucket = bucket
        self._client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url or None)

    def save(self, key: str, content: bytes) -> str:
        key = self._strip_uri(key)
        self._client.put_object(Bucket=self._bucket, Key=key, Body=content)
        return f"s3://{self._bucket}/{key}"

    def read(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=self._strip_uri(key))
        return response["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self._bucket, Key=self._strip_uri(key))
            return True
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code in ("404", "NoSuchKey"):
                return False
            raise

    def delete(self, key: str) -> None:
        # S3's delete_object is idempotent server-side — deleting a missing
        # key is not an error, matching LocalStorageBackend's contract.
        self._client.delete_object(Bucket=self._bucket, Key=self._strip_uri(key))

    @contextmanager
    def local_path(self, key: str) -> Iterator[str]:
        content = self.read(key)
        suffix = Path(self._strip_uri(key)).suffix
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            tmp.write(content)
            tmp.close()
            yield tmp.name
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    @staticmethod
    def _strip_uri(key: str) -> str:
        """Accepts either a bare object key or the full `s3://bucket/key`
        locator `save()` returns, and normalizes to the bare key boto3
        needs. `maxsplit=3` keeps any `/` inside the key itself intact."""
        if key.startswith("s3://"):
            return key.split("/", 3)[-1]
        return key
