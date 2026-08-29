import { Container } from "cloudflare:workers";
import { env as workerEnv } from "cloudflare:workers";

export class Backend extends Container {
  defaultPort = 8000;

  // Long, not "instant-restart": max_instances = 1 in wrangler.toml exists
  // to keep this one process's in-memory state (automation tasks, open
  // browser sessions, WebSocket subscribers - see root Dockerfile) alive.
  // A restart silently drops whatever it was in the middle of.
  sleepAfter = "24h";

  // Cloudflare Worker secrets/vars, forwarded into the container's actual
  // process environment at launch. Secrets set via `wrangler secret put`
  // arrive on `env` the same way `[vars]` does - see DEPLOYMENT.md.
  envVars = {
    DATABASE_URL: workerEnv.DATABASE_URL,
    OPENAI_API_KEY: workerEnv.OPENAI_API_KEY,
    JWT_SECRET: workerEnv.JWT_SECRET,
    ENCRYPTION_KEY: workerEnv.ENCRYPTION_KEY,
    ADZUNA_APP_ID: workerEnv.ADZUNA_APP_ID,
    ADZUNA_APP_KEY: workerEnv.ADZUNA_APP_KEY,
    API_KEY: workerEnv.API_KEY,
    AUTOMATION_BROWSER_MODE: workerEnv.AUTOMATION_BROWSER_MODE,
  };
}

const BACKEND_INSTANCE_NAME = "primary";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname.startsWith("/api/")) {
      // FastAPI's routers are mounted at "/auth", "/resumes", "/chat", etc -
      // never under "/api" (see app/main.py's include_router calls). The
      // frontend and its dev-time Vite proxy both treat "/api" as a prefix
      // that gets stripped before reaching the backend
      // (frontend/vite.config.js's `rewrite`), so this Worker does the same
      // rewrite for the production path.
      const backendUrl = new URL(request.url);
      backendUrl.pathname = url.pathname.slice("/api".length) || "/";
      const backendRequest = new Request(backendUrl, request);

      const backend = env.BACKEND.getByName(BACKEND_INSTANCE_NAME);
      return backend.fetch(backendRequest);
    }

    return env.ASSETS.fetch(request);
  },
};
