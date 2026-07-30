# AI Job Application Agent — Project Report

## What this project does

A **FastAPI** backend that matches uploaded resumes against real job listings and scores them.

### End-to-end flow

1. **Resume upload** (`POST /resumes/upload`) — PDF/DOCX saved to `storage/resumes/`, deduplicated by file hash, record stored in the database.
2. **Text extraction** (`GET /resumes/{id}/extract`) — text pulled from the file via `pdfplumber` / `python-docx`.
3. **Parsing** (`POST /resumes/{id}/parse`) — LLM (OpenAI) parses the raw text into structured data (skills, experience, etc.) with a confidence score.
4. **Embedding** (`POST /resumes/{id}/embed`) — resume text converted to a vector via `sentence-transformers`.
5. **Job ingestion** (`POST /jobs/ingest`) — jobs fetched from the **Adzuna API**, normalized (salary, remote flag, min years experience) and stored.
6. **Job embedding** (`POST /jobs/embed-pending`) — job descriptions embedded and pushed to **Qdrant Cloud** (vector database).
7. **Matching** (`POST /resumes/{id}/matches/generate`) — Qdrant retrieves the most similar jobs, hard filters apply (experience etc.), then a ranker blends vector similarity + skill overlap; a skill-gap analysis and **ATS score** (keywords, format) are computed. Results stored as `match_results`.
8. **Review** (`GET /resumes/{id}/matches`, `PATCH /matches/{id}/status`) — matches listed and marked new / saved / dismissed.

### Architecture

| Layer | Technology |
|---|---|
| API | FastAPI + Uvicorn |
| Relational DB | PostgreSQL via SQLAlchemy ORM + Alembic migrations |
| Vector DB | Qdrant Cloud |
| LLM | OpenAI (parsing) |
| Embeddings | sentence-transformers |
| Job source | Adzuna API |
| Files | Local `storage/resumes/` |

### Database tables (`app/models/db_models.py`)

- **resumes** — file metadata, extracted text, parsed JSON, embedding cache
- **jobs** — Adzuna jobs, normalized fields, embedding cache
- **match_results** — per resume×job scores, matched/missing skills, ATS scores, status

### Where Postgres is used (all of it)

| File | Usage |
|---|---|
| `.env` / `.env.example` | `DATABASE_URL=postgresql://...@localhost:5432/job_agent` |
| `app/core/config.py` | reads `DATABASE_URL` |
| `app/core/database.py` | creates SQLAlchemy engine + sessions |
| `alembic/env.py` | migrations use the same `DATABASE_URL` |
| `requirements.txt` | `psycopg2-binary` driver |

Everything else (repositories, services, API) only talks to SQLAlchemy sessions — no raw Postgres-specific code anywhere.

---

## Migration to Neon Cloud

**Key fact:** Neon *is* PostgreSQL, hosted serverlessly in the cloud. So no logic, models, queries, or migrations need to change — only the connection:

1. `DATABASE_URL` now points to your Neon connection string (with `sslmode=require`).
2. `app/core/database.py` engine tuned for Neon serverless: `pool_pre_ping=True` (Neon suspends idle databases; this transparently reconnects) and `pool_recycle=300`.
3. `psycopg2-binary` stays — it's the driver Neon itself uses.
4. Local Postgres is no longer needed; you can uninstall it.

### What you must do (one time)

1. Go to <https://neon.tech> → sign up (free tier is enough).
2. Create a project (e.g. `job-agent`), pick a region near you.
3. On the project dashboard click **Connect** → copy the connection string. It looks like:
   `postgresql://user:password@ep-xxxx-xxxx.region.aws.neon.tech/neondb?sslmode=require`
4. Paste it as `DATABASE_URL` in `.env` (replacing the placeholder).
5. Run `alembic upgrade head` (or just start the app — `create_all` also creates the tables).

Data starts fresh on Neon (per your choice); local data is not copied.
