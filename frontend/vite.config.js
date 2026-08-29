import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
/// <reference types="vitest" />

// The dev server proxies /api/* to the FastAPI backend, so the frontend
// needs zero CORS configuration and no hardcoded backend URL.
//
// The target is configurable so the same config works both for a bare
// `npm run dev` (backend on localhost) and under docker compose, where the API
// is reachable by service name rather than on 127.0.0.1. Defaults to localhost,
// so nothing changes for the ordinary local workflow.
const API_TARGET = process.env.VITE_API_TARGET || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  // Frontend unit/component tests. `jsdom` because these render real React
  // components; `globals` so `describe`/`it`/`expect` need no import, matching
  // how the backend suite reads.
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.js"],
    // Excluded explicitly: without this, vitest walks node_modules and tries to
    // run every dependency's own test files.
    exclude: ["node_modules/**", "dist/**"],
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: true,
        // The live chat stream is a WebSocket under the same /api prefix.
        // Without this the HTTP proxy answers the upgrade request with a plain
        // 200 and the socket silently never connects — the panel would just sit
        // on its polling fallback forever with no error to explain why.
        ws: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
