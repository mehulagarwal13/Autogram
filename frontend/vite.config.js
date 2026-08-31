import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
/// <reference types="vitest" />

// The dev server proxies /api/* to FastAPI. VITE_API_TARGET configures only
// that local proxy; VITE_API_URL is the browser-visible production API origin.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget = env.VITE_API_TARGET || "http://127.0.0.1:8000";

  return {
    plugins: [react()],
    build: {
      // Cloudflare Pages output directory (relative to frontend/).
      outDir: "dist",
    },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./vitest.setup.js"],
      exclude: ["node_modules/**", "dist/**"],
    },
    server: {
      port: 5173,
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: true,
          // The live chat stream is a WebSocket under the same /api prefix.
          ws: true,
          rewrite: (path) => path.replace(/^\/api/, ""),
        },
      },
    },
  };
});
