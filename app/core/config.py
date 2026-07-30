import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set in .env")

if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
    raise RuntimeError("ADZUNA_APP_ID / ADZUNA_APP_KEY not set in .env")

if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY not set in .env")

# --- Auth (JWT) ---
JWT_SECRET = os.getenv("JWT_SECRET")
if not JWT_SECRET:
    raise RuntimeError("JWT_SECRET not set in .env — generate a long random string.")
JWT_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "1440"))  # 24h default

# --- Field-level encryption (candidate profile PII: phone, address) ---
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY")
if not ENCRYPTION_KEY:
    raise RuntimeError(
        "ENCRYPTION_KEY not set in .env — generate one with:\n"
        '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
    )

# --- Optional: production hardening ---
API_KEY = os.getenv("API_KEY")  # unset = auth disabled (local dev)
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]

# --- Optional: background job sync (scheduler stays off unless queries are set) ---
_sync_queries_raw = os.getenv("JOB_SYNC_QUERIES", "")
JOB_SYNC_QUERIES = [q.strip() for q in _sync_queries_raw.split(";") if q.strip()]
JOB_SYNC_INTERVAL_HOURS = int(os.getenv("JOB_SYNC_INTERVAL_HOURS", "6"))
JOB_SYNC_COUNTRY = os.getenv("JOB_SYNC_COUNTRY", "gb")

# --- Automation / browser engine (Phase 2+, automation/browser/*) ---
# Headless by default; set AUTOMATION_HEADLESS=false to watch the browser run
# (e.g. for the manual-login flow — see ARCHITECTURE.md "No password harvesting").
AUTOMATION_HEADLESS = os.getenv("AUTOMATION_HEADLESS", "true").strip().lower() != "false"
# Encrypted Playwright storage-state (cookies/local-storage) per (user, ATS platform).
AUTOMATION_SESSION_DIR = os.getenv("AUTOMATION_SESSION_DIR", "storage/automation_sessions")
# Screenshots / traces / error logs per application run (see ARCHITECTURE.md §14).
AUTOMATION_LOGS_DIR = os.getenv("AUTOMATION_LOGS_DIR", "logs")
