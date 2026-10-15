import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  timeout: 60_000,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: process.env.WORKMEMORY_ENDPOINT || "http://127.0.0.1:8765",
    channel: "chrome",
    headless: true,
    viewport: { width: 1440, height: 1000 },
  },
  outputDir: "../.cache/playwright",
});
