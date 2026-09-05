"""Extract a reviewable draft without changing saved candidate facts."""
from pathlib import Path
from tempfile import TemporaryDirectory

from app.services.storage import get_storage_backend
from app.services.text_extraction import extract_text
from app.services.resume_parser import parse_resume_text


def draft_from_document(document) -> dict:
    with TemporaryDirectory(prefix="autogram-profile-") as directory:
        path = Path(directory) / ("resume" + Path(document.original_filename).suffix.lower())
        path.write_bytes(get_storage_backend().read(document.stored_path))
        parsed, _ = parse_resume_text(extract_text(str(path)))
    # Sensitive screening answers and inferred skills are deliberately absent.
    return {key: value for key, value in {
        "full_name": parsed.full_name,
        "email": parsed.email,
        "phone": parsed.phone,
        "location": parsed.location,
    }.items() if value is not None and str(value).strip()}
