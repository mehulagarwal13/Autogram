import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Builds ONLY src/sidepanel/ (background.js/content-script.js/manifest.json
// are plain MV3 files, loaded as-is, no build step needed). Root is pinned
// to src/sidepanel so the output lands at dist/sidepanel/index.html —
// exactly what manifest.json's side_panel.default_path points at.
// Unhashed filenames so manifest.json can reference a stable path across
// rebuilds; MV3 extensions are reloaded (not live-served), so cache-busting
// hashes buy nothing here.
export default defineConfig({
  root: "src/sidepanel",
  base: "",
  plugins: [react()],
  build: {
    outDir: "../../dist/sidepanel",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        entryFileNames: "[name].js",
        chunkFileNames: "[name].js",
        assetFileNames: "[name][extname]",
      },
    },
  },
});
