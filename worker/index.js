import { Container } from "cloudflare:workers";
import { env as workerEnv } from "cloudflare:workers";

function compactEnv(values) {
  return Object.fromEntries(
    Object.entries(values).filter(([, value]) => value !== undefined && value !== null && value !== ""),
  );
}

export class Backend extends Container {
  defaultPort = 8000;

  // Long, not "instant-restart": max_instances = 1 in wrangler.toml exists
  // to keep this one process's in-memory state (automation tasks, open
  // browser sessions, WebSocket subscribers - see backend/Dockerfile) alive.
  // A restart silently drops whatever it was in the middle of.
  sleepAfter = "24h";

  // Cloudflare Worker secrets/vars, forwarded into the container's actual
  // process environment at launch. Secrets set via `wrangler secret put`
  // arrive on `env` the same way `[vars]` does - see DEPLOYMENT.md.
  envVars = compactEnv({
    DATABASE_URL: workerEnv.DATABASE_URL,
    OPENAI_API_KEY: workerEnv.OPENAI_API_KEY,
    JWT_SECRET: workerEnv.JWT_SECRET,
    ENCRYPTION_KEY: workerEnv.ENCRYPTION_KEY,
    ADZUNA_APP_ID: workerEnv.ADZUNA_APP_ID,
    ADZUNA_APP_KEY: workerEnv.ADZUNA_APP_KEY,
    API_KEY: workerEnv.API_KEY,
    RATE_LIMIT_PER_MINUTE: workerEnv.RATE_LIMIT_PER_MINUTE,
    CORS_ORIGINS: workerEnv.CORS_ORIGINS,
    CORS_ORIGIN_REGEX: workerEnv.CORS_ORIGIN_REGEX,
    AUTOMATION_BROWSER_MODE: workerEnv.AUTOMATION_BROWSER_MODE,
    AUTOMATION_HEADLESS: workerEnv.AUTOMATION_HEADLESS,
    STORAGE_BACKEND: workerEnv.STORAGE_BACKEND,
    S3_BUCKET: workerEnv.S3_BUCKET,
    S3_REGION: workerEnv.S3_REGION,
    S3_ENDPOINT_URL: workerEnv.S3_ENDPOINT_URL,
    AWS_ACCESS_KEY_ID: workerEnv.AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY: workerEnv.AWS_SECRET_ACCESS_KEY,
  });
}

const BACKEND_INSTANCE_NAME = "primary";

export default {
  async fetch(request, env) {
    // Backend-only service. Cloudflare Pages serves the frontend separately;
    // VITE_API_URL points the browser at this Worker's origin.
    const backend = env.BACKEND.getByName(BACKEND_INSTANCE_NAME);
    return backend.fetch(request);
  },
};
