import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built into the Python package so Rewdio needs no Node at runtime.
export default defineConfig({
  plugins: [react()],
  base: "/static/app/",
  build: { outDir: "../src/rewdio/dashboard/static/app", emptyOutDir: true },
  server: { proxy: { "/api": "http://127.0.0.1:8765", "/static/tokens.css": "http://127.0.0.1:8765" } },
});
