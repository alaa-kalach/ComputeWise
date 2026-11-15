/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // .env lives at the repo root (one file for the whole project); VITE_* values reach the browser.
  envDir: "..",
  server: { port: 5173 },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    env: { VITE_USE_MOCKS: "true" },
  },
});
