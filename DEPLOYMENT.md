# Cloudflare deployment

## Recommended architecture

```text
GitHub (main)
  |-- frontend/** --> Cloudflare Pages --> app.example.com
  `-- backend/** + worker/** --> GitHub Actions --> Worker --> one Container
                                                      `--> api.example.com
```

The React/Vite SPA is a normal Cloudflare Pages project. The Python backend is
not a native Worker: it requires CPython, PostgreSQL/pgvector drivers,
Playwright plus Chromium, background threads, WebSockets, and a filesystem.
The existing FastAPI application therefore runs in a Cloudflare Container,
with `worker/` providing only the required Worker/Durable Object routing layer.

The two deployments are deliberately independent. Pages never builds the
backend image, and the backend workflow never builds or publishes the frontend.

## Frontend: Cloudflare Pages

Create a Pages project from this GitHub repository with these settings:

| Setting | Value |
|---|---|
| Production branch | `main` |
| Root directory | `frontend` |
| Framework preset | Vite (or None) |
| Build command | `npm run build` |
| Build output directory | `dist` |
| Build watch include path | `frontend/*` |
| Production environment variable | `VITE_API_URL=https://api.example.com` |
| Preview environment variable | `VITE_API_URL=https://api.example.com` |

`frontend/public/_redirects` rewrites non-file routes to `index.html`, so direct
loads such as `/applications/123` work with React Router.

Enable preview deployments for non-production branches. Cloudflare will create
both commit-specific and branch-alias `pages.dev` URLs. If previews call the
production API, configure `CORS_ORIGIN_REGEX` on the backend as described below.
For isolated preview data, create a separate preview backend and point the Pages
Preview value of `VITE_API_URL` at it instead.

## Backend: Worker plus Cloudflare Container

The backend service is named `autogram-api` in `worker/wrangler.toml`.
`max_instances = 1` is required: active Playwright browsers, pause/resume
handles, WebSocket subscribers, and rate-limit state currently live inside one
Python process. Do not increase it until that state has been externalized.

Cloudflare Containers require the Workers Paid plan. The deployment builds the
Linux/amd64 image from `backend/Dockerfile`; Docker runs on the GitHub-hosted
runner, so Docker is not required for ordinary local development.

After the first deploy, attach `api.example.com` under the Worker's custom
domains (or use the generated `workers.dev` URL), then set that exact origin as
the Pages `VITE_API_URL`. Attach `app.example.com` to the Pages project if a
custom frontend domain is desired.

Manual backend deployment, when needed, is:

```bash
cd worker
npm ci
npm run deploy
```

Unlike the GitHub-hosted workflow, a manual Container deployment requires a
local Docker-compatible engine to be running.

### GitHub Actions secrets

Add these under GitHub repository Settings > Secrets and variables > Actions:

- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN` with the minimum Workers/Containers edit permissions
- `DATABASE_URL` for the Alembic migration job

The backend workflow is `.github/workflows/deploy-backend.yml`. It runs only
for changes under `backend/`, `worker/`, or the workflow itself, applies Alembic
migrations, then deploys the Worker and Container.

### Cloudflare Worker secrets

Set runtime secrets once from an authenticated machine:

```bash
cd worker
npm install
npx wrangler login
npx wrangler secret put DATABASE_URL
npx wrangler secret put OPENAI_API_KEY
npx wrangler secret put ADZUNA_APP_ID
npx wrangler secret put ADZUNA_APP_KEY
npx wrangler secret put JWT_SECRET
npx wrangler secret put ENCRYPTION_KEY
npx wrangler secret put CORS_ORIGINS
```

`CORS_ORIGINS` is a comma-separated exact allowlist, for example
`https://app.example.com,https://autogram.pages.dev`. To allow Cloudflare Pages
preview hostnames too, set an anchored regex using the actual Pages project
name:

```bash
npx wrangler secret put CORS_ORIGIN_REGEX
# value example:
# ^https://([a-z0-9-]+\.)?autogram\.pages\.dev$
```

Optional runtime settings can also be stored with `wrangler secret put`:

- `RATE_LIMIT_PER_MINUTE`
- `API_KEY` only for machine-client deployments; do not put a shared API key
  in a public Vite bundle
- `S3_BUCKET`, `S3_REGION`, `S3_ENDPOINT_URL`
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`

Non-secret container defaults live in `worker/wrangler.toml`:
`AUTOMATION_BROWSER_MODE=launch`, `AUTOMATION_HEADLESS=true`, and currently
`STORAGE_BACKEND=local`. Do not commit secret values to that file.

## Required environment variables

| Variable | Local backend | Production backend | Frontend |
|---|---:|---:|---:|
| `DATABASE_URL` | required | required secret | no |
| `OPENAI_API_KEY` | required | required secret | no |
| `ADZUNA_APP_ID` | required | required secret | no |
| `ADZUNA_APP_KEY` | required | required secret | no |
| `JWT_SECRET` | required | required secret | no |
| `ENCRYPTION_KEY` | required | required secret | no |
| `CORS_ORIGINS` | not needed with Vite proxy | required exact frontend origin(s) | no |
| `CORS_ORIGIN_REGEX` | optional | optional Pages previews | no |
| `VITE_API_URL` | optional; blank uses `/api` | no | required Pages build value |
| `VITE_API_TARGET` | optional; defaults to port 8000 | no | dev server only |

See `backend/.env.example`, `frontend/.env.example`, and
`worker/.dev.vars.example` for the complete optional configuration.

## Local development

Backend terminal (PowerShell shown):

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r backend/requirements.txt
Copy-Item backend/.env.example backend/.env
Set-Location backend
alembic upgrade head
uvicorn app.main:app --reload
```

Frontend terminal:

```powershell
Set-Location frontend
npm install
Copy-Item .env.example .env.local
npm run dev
```

Leave `VITE_API_URL` blank locally. Vite proxies `/api` and WebSockets to
`VITE_API_TARGET` (`http://127.0.0.1:8000` by default).

## Deployment flow

For normal production changes:

```bash
git add .
git commit -m "my change"
git push origin main
```

- A `frontend/*` change triggers the connected Pages project.
- A `backend/*` or `worker/*` change triggers the backend GitHub Action.
- A commit that changes both triggers both independently.
- Pull-request branches receive Pages previews. The backend is production-only
  by default; use a separate Cloudflare backend service/database if backend PR
  previews are required.

After a backend deployment, verify `GET https://api.example.com/health` and a
WebSocket-backed chat/automation screen. After a frontend deployment, verify a
direct refresh on a nested SPA route.

## Rollback

Frontend: Cloudflare Pages > project > Deployments > select a previous
successful production deployment > **Rollback to this deployment**. Preview
deployments are not rollback targets.

Backend: Cloudflare Workers & Pages > `autogram-api` > Deployments > choose a
previous version > **Rollback**, or run from `worker/`:

```bash
npx wrangler rollback
```

Worker rollback changes code/container version but does not reverse PostgreSQL
migrations or external storage changes. Database migrations must remain
backward-compatible (expand before contract); use a reviewed forward migration
for data/schema rollback rather than blindly downgrading production.

## Current limitations

- The backend is intentionally limited to one Container instance. This avoids
  corrupt pause/resume behavior but limits horizontal scale and availability.
- Cloudflare can stop or move a Container. In-flight automation is reconciled
  as interrupted on restart; a Container is not guaranteed to run forever.
- `STORAGE_BACKEND=local` keeps existing file behavior but Container disk is
  ephemeral. The S3/R2 backend exists, but the autonomous-agent upload allowlist
  still requires a local file path, so switching fully to R2 needs a small file
  materialization lifecycle change before all automation modes are equivalent.
- Browser automation in production is headless. The local CDP mode that attaches
  to a user's already logged-in Chrome is not available in the cloud Container.
- The backend workflow deploys production only. A separate Worker name,
  database, and secrets are required for safe backend preview environments.
