import { existsSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { defineConfig, type LaunchOptions } from "@playwright/test";

/**
 * Locate a Chromium to drive.
 *
 * Playwright normally downloads a build matched to its own version. Some CI
 * images (including this project's) ship a pre-installed Chromium under
 * PLAYWRIGHT_BROWSERS_PATH whose build number does not match, and downloading is
 * disabled. So: honour an explicit override, then look for a pre-installed
 * chromium, and otherwise fall back to Playwright's own managed browser.
 */
function findChromium(): string | undefined {
  const override = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE;
  if (override && existsSync(override)) return override;

  const root = process.env.PLAYWRIGHT_BROWSERS_PATH;
  if (!root || !existsSync(root)) return undefined;

  const candidates: string[] = [];
  for (const entry of readdirSync(root)) {
    if (!entry.startsWith("chromium")) continue;
    candidates.push(join(root, entry, "chrome-linux", "chrome"));
    candidates.push(join(root, entry, "chrome-linux", "headless_shell"));
  }
  // Prefer a full chrome over headless_shell: the PWA tests need a real browser
  // (service workers, manifest handling), not just a rendering shell.
  return candidates.find((path) => existsSync(path));
}

const executablePath = findChromium();
const launchOptions: LaunchOptions = executablePath ? { executablePath } : {};

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:4173",
    headless: true,
    trace: "off",
    launchOptions,
  },
  webServer: {
    command: "npm run preview -- --host 127.0.0.1 --port 4173",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
