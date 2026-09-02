# Deploying to Railway (frontend and backend as separate services)

Two independent Railway services built from this one GitHub repo:

```text
GitHub (main)
  |-- frontend/  --> Railway service "frontend"  (Docker: Vite build -> nginx)  --> browser
  |                                                                                   |
  |                                                                     VITE_API_URL (build-time)
  |                                                                                   |
  `-- backend/   --> Railway service "backend"   (Docker: FastAPI + Chromium)  <-------'
                                                        |
                                          Neon PostgreSQL / OpenAI / Adzuna
```

Both services live in **one Railway project**, each with its own **Root Directory**
so a push only rebuilds the service whose files changed. The config already in the
repo:

| File | Purpose |
|---|---|
| `backend/railway.json` | Docker build, `/health` check, **1 replica**, no sleep |
| `backend/Dockerfile` | Existing image — runs `alembic upgrade head` then `uvicorn` on `$PORT` |
| `frontend/railway.json` | Docker build |
| `frontend/Dockerfile` | `npm ci && npm run build`, then nginx serving `dist/` on `$PORT` |
| `frontend/nginx.conf.template` | SPA fallback: every route resolves to `index.html` |

> **Do not scale the backend past 1 replica.** Automation task handles, open
> browsers, WebSocket subscribers, and the in-memory rate limiter all live in
> process memory — see the single-worker section in [README.md](./README.md).
> `numReplicas: 1` in `backend/railway.json` enforces this; keep it.

---

## Prerequisites

- A Railway account and the repo pushed to GitHub.
- **Neon** (or any) PostgreSQL URL with `?sslmode=require`. Railway can also host
  Postgres (add a Postgres service and use its `DATABASE_URL`), but pgvector must
  be available — Neon has it on by default; Railway's Postgres image also ships
  the `vector` extension and the app runs `CREATE EXTENSION IF NOT EXISTS vector`
  at startup.
- `OPENAI_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`.
- A Fernet `ENCRYPTION_KEY`:
  ```bash
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  ```
- A long random `JWT_SECRET`:
  ```bash
  python -c "import secrets; print(secrets.token_urlsafe(48))"
  ```

---

## 1. Create the project and the backend service

1. **New Project → Deploy from GitHub repo →** select this repo.
2. Railway creates one service. Open it → **Settings**:
   - **Service name:** `backend` (exact name matters — the frontend references it).
   - **Root Directory:** `backend`
   - **Build:** it will auto-detect `backend/Dockerfile` / `backend/railway.json`.
     Leave the builder on Dockerfile.
3. **Settings → Networking → Generate Domain.** Railway picks the container port
   from the Dockerfile's `EXPOSE 10000`; if it asks, enter **10000**.
4. **Variables** tab — add:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | your Neon URL incl. `?sslmode=require` |
   | `OPENAI_API_KEY` | your key |
   | `ADZUNA_APP_ID` | your id |
   | `ADZUNA_APP_KEY` | your key |
   | `JWT_SECRET` | generated above |
   | `ENCRYPTION_KEY` | Fernet key generated above |
   | `AUTOMATION_BROWSER_MODE` | `launch` |
   | `AUTOMATION_HEADLESS` | `true` |
   | `STORAGE_BACKEND` | `local` |
   | `RATE_LIMIT_PER_MINUTE` | `60` |
   | `CORS_ORIGINS` | `https://${{frontend.RAILWAY_PUBLIC_DOMAIN}}` |

   `CORS_ORIGINS` uses a **reference variable** — it resolves to the frontend
   service's domain once that service exists (step 2). Don't set `PORT`; the
   Dockerfile defaults to 10000 and Railway routes to it. Don't set `API_KEY`
   (it would gate the browser UI behind a shared header).

5. Let it deploy. First build is slow (Torch CPU wheels + Chromium headless
   shell). The deploy is healthy once `GET /health` returns 200 — which only
   happens after `alembic upgrade head` succeeds, so a bad migration fails the
   deploy instead of shipping a broken revision.

### Optional but recommended: a volume for uploads

Railway containers have an **ephemeral filesystem** — résumé uploads,
screenshots, logs, and the downloaded embedding model are wiped on every deploy
and restart. To persist them:

- **Settings → Volumes → New Volume**, mount path `/app/storage`.
- (Optional second volume) mount `/app/logs` for run traces.

Postgres rows always survive; only local file blobs are at risk without a volume.

---

## 2. Add the frontend service

1. In the **same project**: **New → GitHub Repo →** the same repo again
   (or **New → Empty Service** then connect the repo).
2. Open the new service → **Settings**:
   - **Service name:** `frontend`
   - **Root Directory:** `frontend`
   - Builder: Dockerfile (auto-detected from `frontend/Dockerfile`).
3. **Settings → Networking → Generate Domain.** If asked for a port, enter
   **8080** (the Dockerfile's `EXPOSE`); Railway still injects the real `$PORT`
   and nginx binds it via the template.
4. **Variables** tab — add:

   | Variable | Value |
   |---|---|
   | `VITE_API_URL` | `https://${{backend.RAILWAY_PUBLIC_DOMAIN}}` |

   This is consumed **at build time** (Vite inlines it). Railway passes service
   variables to the Docker build as args automatically, and the Dockerfile
   declares `ARG VITE_API_URL`. It must NOT end with a slash and must include
   `https://`.

5. Deploy.

---

## 3. Reconnect CORS and redeploy

After both services have domains:

- The backend's `CORS_ORIGINS` reference resolves to the real frontend URL.
  **Redeploy the backend** once (Deployments → ⋯ → Redeploy) so the running
  process picks up the resolved value.
- If you later add a **custom domain** to the frontend, append it:
  `CORS_ORIGINS=https://${{frontend.RAILWAY_PUBLIC_DOMAIN}},https://app.example.com`
  and redeploy the backend.

WebSocket chat streams need no extra config: `frontend/src/api.js` derives the
`wss://` URL from `VITE_API_URL`, and Railway upgrades WebSockets on the same
domain.

---

## 4. Verify

```bash
curl --fail https://<backend-domain>/health
```

Then in the browser at the frontend domain: sign up / log in, refresh a nested
route (e.g. `/applications/123` — should not 404), upload a résumé, open a chat
stream, run one small automation. Check the browser devtools Network tab shows
requests going to the backend domain with no CORS errors.

---

## 5. Continuous deploys

Railway auto-deploys on push to the connected branch. With Root Directories set,
a `frontend/**` change rebuilds only the frontend and a `backend/**` change only
the backend. Changes to a service's `railway.json` / `Dockerfile` redeploy that
service. Keep API changes backward-compatible — the two services deploy
independently and can briefly run different revisions.

---

## CLI alternative

```bash
npm i -g @railway/cli
railway login
railway link            # pick the project

# backend
railway service          # select "backend", or: railway up --service backend
railway variables --set DATABASE_URL=... --set OPENAI_API_KEY=...   # etc.
railway up --service backend

# frontend
railway variables --set 'VITE_API_URL=https://<backend-domain>' --service frontend
railway up --service frontend
```

Root Directory and generated domains are still set in the dashboard (or with
`railway service` settings). `railway up` uploads the repo and builds with the
same `railway.json` config.

---

## Notes and limits

- **Image size / build time:** the backend image carries PyTorch (CPU) and the
  Playwright Chromium headless shell — expect a multi-hundred-MB image and a
  several-minute first build. Later builds reuse Railway's layer cache.
- **Cold starts:** `sleepApplication: false` keeps both services always-on
  (Railway's usage-based billing still applies). If you flip the backend to
  sleep, an incoming request wakes it but any in-flight automation is lost.
- **Single backend replica** is a hard architectural requirement, not a cost
  choice — see README.md.
- **Migrations** run in the backend container's start command
  (`alembic upgrade head`), because that keeps a failed migration from becoming
  a healthy deploy. They are not idempotent to *downgrade* — keep migrations
  expand-then-contract.

### Railway reference links

- Config as code: https://docs.railway.com/reference/config-as-code
- Monorepo / root directory: https://docs.railway.com/guides/monorepo
- Dockerfile builds: https://docs.railway.com/guides/dockerfiles
- Variables & references: https://docs.railway.com/guides/variables
- Volumes: https://docs.railway.com/reference/volumes
- Healthchecks: https://docs.railway.com/reference/healthchecks
