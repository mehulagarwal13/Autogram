"""
Shared behavioral contract for every StorageBackend implementation, run
against LocalStorageBackend directly and against S3StorageBackend backed by
a moto-mocked bucket (no real AWS calls). If a new backend is added later
(e.g. Google Cloud Storage), it only needs a fixture added to
`ALL_BACKENDS` below to be held to the same contract.

Also covers the factory (`get_storage_backend`) and the two rewired
consumer modules (file_storage.py, document_storage.py) end-to-end against
a temp directory, to confirm the local-mode refactor is byte-for-byte
behavior-preserving.
"""

from __future__ import annotations

import os
import uuid

import pytest
from moto import mock_aws

from app.services.storage.base import StorageBackend
from app.services.storage.local_backend import LocalStorageBackend
from app.services.storage.s3_backend import S3StorageBackend


# ---------------------------------------------------------------------------
# Fixtures: one per backend, both yielding a plain StorageBackend instance
# ---------------------------------------------------------------------------

@pytest.fixture
def local_backend(tmp_path) -> StorageBackend:
    return LocalStorageBackend()


@pytest.fixture
def s3_backend() -> StorageBackend:
    with mock_aws():
        import boto3

        bucket = "autogram-test-bucket"
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=bucket)
        yield S3StorageBackend(bucket=bucket, region="us-east-1")


def _key_for(tmp_path, backend) -> str:
    """Local needs a real path under tmp_path; S3 just needs a key string —
    both are valid inputs to save(), so branch on backend type here rather
    than parametrizing fixtures against incompatible key shapes."""
    unique = f"{uuid.uuid4()}.pdf"
    if isinstance(backend, LocalStorageBackend):
        return str(tmp_path / "resumes" / unique)
    return f"resumes/{unique}"


ALL_BACKEND_FIXTURES = ["local_backend", "s3_backend"]


# ---------------------------------------------------------------------------
# Shared contract — parametrized across every backend
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("backend_fixture", ALL_BACKEND_FIXTURES)
def test_save_then_read_round_trips(backend_fixture, request, tmp_path):
    backend = request.getfixturevalue(backend_fixture)
    key = _key_for(tmp_path, backend)
    content = b"%PDF-1.4 fake resume bytes"

    locator = backend.save(key, content)

    assert backend.read(locator) == content


@pytest.mark.parametrize("backend_fixture", ALL_BACKEND_FIXTURES)
def test_exists_true_after_save_false_before(backend_fixture, request, tmp_path):
    backend = request.getfixturevalue(backend_fixture)
    key = _key_for(tmp_path, backend)

    assert backend.exists(key) is False

    locator = backend.save(key, b"content")

    assert backend.exists(locator) is True


@pytest.mark.parametrize("backend_fixture", ALL_BACKEND_FIXTURES)
def test_delete_removes_content(backend_fixture, request, tmp_path):
    backend = request.getfixturevalue(backend_fixture)
    key = _key_for(tmp_path, backend)
    locator = backend.save(key, b"content")

    backend.delete(locator)

    assert backend.exists(locator) is False


@pytest.mark.parametrize("backend_fixture", ALL_BACKEND_FIXTURES)
def test_delete_of_missing_key_is_not_an_error(backend_fixture, request, tmp_path):
    backend = request.getfixturevalue(backend_fixture)
    key = _key_for(tmp_path, backend)

    backend.delete(key)  # never saved — must not raise


@pytest.mark.parametrize("backend_fixture", ALL_BACKEND_FIXTURES)
def test_local_path_yields_readable_real_file(backend_fixture, request, tmp_path):
    backend = request.getfixturevalue(backend_fixture)
    key = _key_for(tmp_path, backend)
    content = b"%PDF-1.4 fake resume bytes"
    locator = backend.save(key, content)

    with backend.local_path(locator) as path:
        assert os.path.isfile(path)
        with open(path, "rb") as f:
            assert f.read() == content


def test_s3_local_path_cleans_up_temp_file_on_exit(s3_backend):
    locator = s3_backend.save("resumes/cleanup-check.pdf", b"content")

    with s3_backend.local_path(locator) as path:
        assert os.path.isfile(path)

    assert not os.path.isfile(path)  # temp file removed after the context exits


def test_s3_backend_accepts_bare_key_or_full_uri_interchangeably(s3_backend):
    locator = s3_backend.save("resumes/uri-check.pdf", b"content")
    bare_key = "resumes/uri-check.pdf"

    assert s3_backend.read(locator) == s3_backend.read(bare_key) == b"content"


# ---------------------------------------------------------------------------
# Factory (get_storage_backend) — selection + singleton caching
# ---------------------------------------------------------------------------

def test_factory_defaults_to_local(monkeypatch):
    from app.services import storage

    storage.reset_storage_backend()
    monkeypatch.setattr("app.core.config.STORAGE_BACKEND", "local", raising=False)

    backend = storage.get_storage_backend()

    assert isinstance(backend, LocalStorageBackend)
    storage.reset_storage_backend()


def test_factory_caches_singleton(monkeypatch):
    from app.services import storage

    storage.reset_storage_backend()
    monkeypatch.setattr("app.core.config.STORAGE_BACKEND", "local", raising=False)

    first = storage.get_storage_backend()
    second = storage.get_storage_backend()

    assert first is second
    storage.reset_storage_backend()


def test_factory_selects_s3_backend_when_configured(monkeypatch):
    from app.services import storage

    storage.reset_storage_backend()
    monkeypatch.setattr("app.core.config.STORAGE_BACKEND", "s3", raising=False)
    monkeypatch.setattr("app.core.config.S3_BUCKET", "some-bucket", raising=False)
    monkeypatch.setattr("app.core.config.S3_REGION", "us-east-1", raising=False)
    monkeypatch.setattr("app.core.config.S3_ENDPOINT_URL", None, raising=False)

    with mock_aws():
        backend = storage.get_storage_backend()
        assert isinstance(backend, S3StorageBackend)

    storage.reset_storage_backend()


# ---------------------------------------------------------------------------
# Consumer modules end-to-end: file_storage.py / document_storage.py against
# LocalStorageBackend (the default), confirming the refactor changed nothing
# observable about their existing public contract.
# ---------------------------------------------------------------------------

def test_save_resume_file_still_returns_resume_id_and_local_path(tmp_path, monkeypatch):
    from app.services import file_storage, storage

    storage.reset_storage_backend()
    monkeypatch.chdir(tmp_path)  # file_storage.STORAGE_DIR is relative — isolate per test

    resume_id, stored_path = file_storage.save_resume_file(
        "resume.pdf", b"%PDF-1.4 fake resume"
    )

    assert uuid.UUID(resume_id)  # a valid UUID was generated
    assert stored_path.endswith(f"{resume_id}.pdf")
    assert os.path.isfile(stored_path)
    with open(stored_path, "rb") as f:
        assert f.read() == b"%PDF-1.4 fake resume"

    storage.reset_storage_backend()


def test_save_document_file_still_returns_document_id_and_local_path(tmp_path, monkeypatch):
    from app.services import document_storage, storage

    storage.reset_storage_backend()
    monkeypatch.chdir(tmp_path)

    document_id, stored_path = document_storage.save_document_file(
        "cover_letter", "letter.pdf", b"%PDF-1.4 fake letter"
    )

    assert uuid.UUID(document_id)
    assert "cover_letter" in stored_path
    assert os.path.isfile(stored_path)

    storage.reset_storage_backend()


def test_delete_document_file_removes_it(tmp_path, monkeypatch):
    from app.services import document_storage, storage

    storage.reset_storage_backend()
    monkeypatch.chdir(tmp_path)

    _, stored_path = document_storage.save_document_file(
        "other", "note.txt", b"plain text content"
    )
    assert os.path.isfile(stored_path)

    document_storage.delete_document_file(stored_path)

    assert not os.path.isfile(stored_path)
    storage.reset_storage_backend()
