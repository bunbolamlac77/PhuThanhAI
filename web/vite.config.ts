import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Offline-first: no external CDN, no remote fonts, relative asset base so the
// bundle also works when loaded from WKWebView via the local backend.
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    target: "es2022",
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: false,
  },
});
