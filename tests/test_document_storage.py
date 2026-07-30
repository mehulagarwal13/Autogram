"""
Document upload validation (extension + magic-byte checks) for the master
profile's resume/cover-letter/certificate storage. Pure functions — no disk
writes are exercised here (save_document_file is covered indirectly by the
profile API in integration testing against a real Postgres instance).
"""

import pytest

from app.services.document_storage import validate_content, validate_extension

PDF_BYTES = b"%PDF-1.4 rest of a fake pdf"
DOCX_BYTES = b"PK\x03\x04 rest of a fake docx"
PNG_BYTES = b"\x89PNG\r\n\x1a\n rest of a fake png"


def test_resume_accepts_pdf_and_docx():
    assert validate_extension("resume", "resume.pdf") == ".pdf"
    assert validate_extension("resume", "resume.docx") == ".docx"


def test_resume_rejects_png():
    with pytest.raises(ValueError):
        validate_extension("resume", "resume.png")


def test_certificate_accepts_image_formats():
    assert validate_extension("certificate", "cert.png") == ".png"
    assert validate_extension("certificate", "cert.jpg") == ".jpg"


def test_unknown_document_type_rejected():
    with pytest.raises(ValueError):
        validate_extension("passport_scan", "file.pdf")


def test_content_must_match_claimed_extension():
    # A renamed .exe (or any non-PDF content) claiming to be a .pdf must be rejected.
    with pytest.raises(ValueError):
        validate_content(".pdf", b"MZ\x90\x00 this is actually an exe")


def test_content_matching_extension_passes():
    validate_content(".pdf", PDF_BYTES)
    validate_content(".docx", DOCX_BYTES)
    validate_content(".png", PNG_BYTES)


def test_txt_has_no_magic_byte_requirement():
    # .txt has no reliable signature — any content is accepted once the
    # extension itself is allowed for that document type.
    validate_content(".txt", b"anything at all")
