from app.models.db_models import JobRecord


def build_job_summary_text(job: JobRecord) -> str:
    parts = []
    if job.title:
        parts.append(job.title + ".")
    if job.company:
        parts.append(f"At {job.company}.")
    if job.location:
        parts.append(f"Location: {job.location}.")
    if job.description:
        parts.append(job.description)
    return " ".join(parts)