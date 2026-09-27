import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { readFileSync } from "node:fs";

export default defineConfig({
  plugins: [react(), {
    name: "frontend-version",
    generateBundle() {
      this.emitFile({ type: "asset", fileName: "version.json",
        source: readFileSync(new URL("./src/version.json", import.meta.url), "utf8") });
    }
  }],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000"
    }
  }
});
