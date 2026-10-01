import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

export default defineConfig({
  plugins: [react(), {
    name: "frontend-version",
    generateBundle() {
      this.emitFile({ type: "asset", fileName: "version.json",
        source: readFileSync(new URL("./src/version.json", import.meta.url), "utf8") });
    },
    writeBundle(options, bundle) {
      const { version } = JSON.parse(readFileSync(new URL("./src/version.json", import.meta.url), "utf8"));
      const urls = ["/index.html", "/manifest.webmanifest", "/apple-touch-icon.png", "/logo.png",
        ...Object.keys(bundle).filter((name) => name.startsWith("assets/")).map((name) => `/${name}`)];
      const worker = readFileSync(new URL("./public/sw.js", import.meta.url), "utf8")
        .replace("__BUILD_VERSION__", version)
        .replace("/* PRECACHE */ []", JSON.stringify(urls));
      writeFileSync(resolve(options.dir ?? "dist", "sw.js"), worker);
    }
  }],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000"
    }
  }
});
