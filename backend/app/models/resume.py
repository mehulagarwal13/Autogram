from pydantic import BaseModel
from datetime import datetime


class ResumeUploadResponse(BaseModel):
    resume_id: str
    original_filename: str
    stored_path: str
    uploaded_at: datetime
    status: str  # "uploaded" for now; later: "parsed", "failed", etc.