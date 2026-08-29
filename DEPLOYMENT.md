# Deploying to Cloudflare

Everything lives behind **one Cloudflare Worker** (`worker/`): it serves the
built frontend as static assets and proxies `/api/*` to the backend, which
runs as a **Cloudflare Container** built from the root `Dockerfile`. One
hostname for both pieces means the frontend's existing same-origin
assumptions (relative `/api/...` fetches, WebSocket built from
`window.location.host`) keep working with zero frontend code changes.

Deploys are triggered by pushing to `main` — see
`.github/workflows/deploy.yml`. The Docker build for the backend happens on
GitHub's runner; **you never need Docker installed locally.**

## Architectural constraints this setup respects

- **The backend must stay exactly one instance** (`max_instances = 1` in
  `worker/wrangler.toml`). It keeps automation task state, open
  review-session browsers, and WebSocket subscribers in that one process's
  memory — see `README.md`'s "SINGLE WORKER ONLY" section. Do not raise
  `max_instances`.
- **Browser automation runs headless (`AUTOMATION_BROWSER_MODE=launch`) in
  the cloud**, not the local-dev default `cdp` mode. `cdp` attaches to a
  *developer's own* running Chrome so a human can watch a run and solve a
  CAPTCHA/OTP live — there is no such browser to attach to in the cloud, so
  that live-watch workflow is unavailable in this deployment. CAPTCHA/OTP
  handling in the cloud goes through the existing human-interaction-request
  API flow instead.
- **Known follow-up, not yet done**: `AUTOMATION_SESSION_DIR`,
  `AUTOMATION_LOGS_DIR`, and `AUTOMATION_CHROME_USER_DATA_DIR` write to local
  disk inside the container, which is not guaranteed durable across
  restarts/redeploys. Encrypted sessions, screenshots, and traces can be lost
  on a restart until these are moved to R2 (the S3-compatible backend already
  implemented in `app/services/storage/s3_backend.py`).

## One-time setup

1. **Cloudflare**: create an API token (needs Workers Scripts edit +
   Cloudflare Containers edit permissions) and note your Account ID, both
   from the Cloudflare dashboard.

2. **GitHub repo secrets** (Settings → Secrets and variables → Actions):
   - `CLOUDFLARE_API_TOKEN`
   - `CLOUDFLARE_ACCOUNT_ID`
   - `DATABASE_URL` — used only by the migration step in CI, same Neon URL as
     local dev.

3. **Cloudflare Worker secrets** — the backend's actual runtime secrets,
   forwarded into the container by `worker/index.js`'s `envVars`. Set these
   once from your machine (needs Node, still no Docker):
   ```bash
   cd worker
   npm install
   npx wrangler login
   npx wrangler secret put DATABASE_URL
   npx wrangler secret put OPENAI_API_KEY
   npx wrangler secret put JWT_SECRET
   npx wrangler secret put ENCRYPTION_KEY
   npx wrangler secret put ADZUNA_APP_ID
   npx wrangler secret put ADZUNA_APP_KEY
   npx wrangler secret put API_KEY   # optional, enables X-API-Key auth
   ```
   Non-secret config (currently just `AUTOMATION_BROWSER_MODE=launch`) lives
   in `worker/wrangler.toml`'s `[vars]` block instead.

4. **Push to `main`.** The workflow builds `frontend/dist`, runs
   `alembic upgrade head` against Neon, then `wrangler deploy` builds the
   container image and publishes both the container and the static assets
   behind the Worker.

## Verifying a deploy

- Load the Worker's URL — the frontend should render.
- `GET /health` (routed through the Worker to the container) should report
  the database as reachable.
- Open a page that uses the live automation WebSocket to confirm the
  `/api/*` → container proxy handles the upgrade correctly, not just plain
  HTTP.
- `SELECT * FROM alembic_version;` against Neon to confirm the migration step
  actually ran before trusting the deployed schema.

## Local development is unaffected

`docker-compose.yml` still runs the API and Vite dev server locally exactly
as before (`AUTOMATION_BROWSER_MODE` defaults to `cdp`, attaching to your own
Chrome). None of the files above change how `npm run dev` or
`uvicorn app.main:app --reload` behave locally.
