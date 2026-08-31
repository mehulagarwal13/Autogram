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
CORS_ORIGIN_REGEX = os.getenv("CORS_ORIGIN_REGEX") or None

# --- Optional: background job sync (scheduler stays off unless queries are set) ---
_sync_queries_raw = os.getenv("JOB_SYNC_QUERIES", "")
JOB_SYNC_QUERIES = [q.strip() for q in _sync_queries_raw.split(";") if q.strip()]
JOB_SYNC_INTERVAL_HOURS = int(os.getenv("JOB_SYNC_INTERVAL_HOURS", "6"))
JOB_SYNC_COUNTRY = os.getenv("JOB_SYNC_COUNTRY", "gb")

# --- Automation / browser engine (Phase 2+, automation/browser/*) ---
# How BrowserManager gets a browser. See automation/browser/chrome_attach.py.
#   "cdp"        (default) Attach to the user's ALREADY-RUNNING Google Chrome over
#                the Chrome DevTools Protocol and open the job in a NEW TAB, reusing
#                their real profile — cookies, Gmail/LinkedIn/Workday/Greenhouse
#                logins, everything. Nothing is listening on the debug port? Chrome
#                is started with the port open (AUTOMATION_CDP_AUTOLAUNCH) and left
#                running, so later runs attach to that one. Falls back to
#                "persistent" if Chrome can't be attached to at all.
#   "persistent" Skip CDP; open a normal (NOT incognito) window on a real, reusable
#                on-disk profile directory. Cookies/logins persist across runs.
#   "launch"     The original Phase 2 behavior: a throwaway browser with an empty,
#                incognito-equivalent context seeded from the encrypted
#                storage-state in AUTOMATION_SESSION_DIR. Keep this for CI/headless
#                servers where there is no user Chrome to attach to.
AUTOMATION_BROWSER_MODE = os.getenv("AUTOMATION_BROWSER_MODE", "cdp").strip().lower()
if AUTOMATION_BROWSER_MODE not in ("cdp", "persistent", "launch"):
    raise RuntimeError(
        "AUTOMATION_BROWSER_MODE must be 'cdp', 'persistent' or 'launch', "
        f"got: {AUTOMATION_BROWSER_MODE!r}"
    )
# Chrome's DevTools endpoint. Accepts "9222", "localhost:9222" or a full URL.
AUTOMATION_CDP_URL = os.getenv("AUTOMATION_CDP_URL", "http://127.0.0.1:9222").strip()
# May we start Chrome ourselves (with --remote-debugging-port) when nothing is
# listening? Set to false to require that the user starts Chrome with the flag.
AUTOMATION_CDP_AUTOLAUNCH = os.getenv("AUTOMATION_CDP_AUTOLAUNCH", "true").strip().lower() != "false"
# How long to wait for a Chrome we started to open the debug port.
AUTOMATION_CDP_LAUNCH_TIMEOUT_S = float(os.getenv("AUTOMATION_CDP_LAUNCH_TIMEOUT_S", "30"))
# Full path to chrome.exe / Google Chrome. Unset = auto-detect the standard
# install locations for this OS.
AUTOMATION_CHROME_PATH = os.getenv("AUTOMATION_CHROME_PATH") or None
# Profile directory used when WE start the browser (autolaunch or "persistent").
# A per-Autogram-user subdirectory is created underneath it, so two users of this
# deployment never share a cookie jar. The special value "chrome-default" points
# at the user's REAL Chrome profile — that only works while Chrome is completely
# closed (Chrome's profile lock means a second process on a live profile never
# opens the debug port), so it is opt-in, not the default.
AUTOMATION_CHROME_USER_DATA_DIR = os.getenv("AUTOMATION_CHROME_USER_DATA_DIR", "storage/chrome_profile")
# Headless by default; set AUTOMATION_HEADLESS=false to watch the browser run
# (e.g. for the manual-login flow — see ARCHITECTURE.md "No password harvesting").
# Only consulted in AUTOMATION_BROWSER_MODE=launch: attaching to a human's Chrome
# (or opening a window they may take over for copilot review) is visible by
# definition, so "cdp"/"persistent" runs are never headless.
AUTOMATION_HEADLESS = os.getenv("AUTOMATION_HEADLESS", "true").strip().lower() != "false"
# Encrypted Playwright storage-state (cookies/local-storage) per (user, ATS platform).
AUTOMATION_SESSION_DIR = os.getenv("AUTOMATION_SESSION_DIR", "storage/automation_sessions")
# Screenshots / traces / error logs per application run (see ARCHITECTURE.md §14).
AUTOMATION_LOGS_DIR = os.getenv("AUTOMATION_LOGS_DIR", "logs")
# §9 data retention — how often the purge job runs. Unlike JOB_SYNC_*, this
# is NOT opt-in: retention windows have safe defaults (see
# app/services/retention_repository.py) and the job always runs, so there is
# no env var that turns it off entirely — only how often.
RETENTION_PURGE_INTERVAL_HOURS = int(os.getenv("RETENTION_PURGE_INTERVAL_HOURS", "24"))
# Vision fallback (automation/forms/vision_fallback.py): after every cheaper
# pass has run, screenshot the required fields that are STILL empty and ask a
# vision model to read them. On by default — the fields it exists for (a
# conditional follow-up whose meaning is in the question above it, a control
# whose visible value isn't in its own value) are otherwise left blank and send
# the whole run to a human. Set AUTOMATION_VISION_FALLBACK=false to turn it off;
# it is the most expensive path in the system (one high-detail image per field,
# capped at vision_fallback.MAX_FIELDS_PER_CALL), so a deployment watching cost
# may reasonably want it off.
AUTOMATION_VISION_FALLBACK = os.getenv("AUTOMATION_VISION_FALLBACK", "true").strip().lower() != "false"
# HITL platform: how long ApplicationFlowManager waits, polling, for a human
# to clear a detected CAPTCHA/human-gate before giving up and falling back to
# manual_required (see application_flow_manager.py's "wait-and-resume"). Long
# enough for someone who's actually watching to solve a CAPTCHA; short enough
# that an unattended run doesn't tie up a browser/thread indefinitely.
AUTOMATION_HUMAN_WAIT_TIMEOUT_S = float(os.getenv("AUTOMATION_HUMAN_WAIT_TIMEOUT_S", "600"))

# --- Object storage (Phase 2 hardening — see PHASE2_ARCHITECTURE.md Initiative 4) ---
# "local" (default) keeps today's on-disk behavior under storage/. "s3" routes
# app/services/file_storage.py and document_storage.py through an S3-compatible
# bucket (AWS S3, Cloudflare R2, MinIO) instead — see app/services/storage/.
# Read app/services/storage/s3_backend.py's module docstring before flipping
# this to "s3": text extraction and the Playwright resume-upload path aren't
# rewired to the new local_path() accessor yet, so that specific gap is
# tracked, not silently broken.
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local").strip().lower()
if STORAGE_BACKEND not in ("local", "s3"):
    raise RuntimeError(f"STORAGE_BACKEND must be 'local' or 's3', got: {STORAGE_BACKEND!r}")

S3_BUCKET = os.getenv("S3_BUCKET")
S3_REGION = os.getenv("S3_REGION")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL")  # set for Cloudflare R2 / MinIO; unset for AWS S3

if STORAGE_BACKEND == "s3" and not S3_BUCKET:
    raise RuntimeError("STORAGE_BACKEND=s3 requires S3_BUCKET to be set in .env.")
