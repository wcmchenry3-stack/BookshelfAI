import { defineConfig, devices } from '@playwright/test';

/**
 * Web e2e suite (#470). It drives the static web export (`dist/`, built by
 * `npm run e2e:build`) against a real backend on a scratch Postgres, with a
 * fake AI provider and fake catalog. See docs/testing.md "Web e2e (Playwright)".
 *
 * The two web servers below are started here unless already running
 * (`reuseExistingServer`). `DATABASE_URL` must name a scratch, migrated
 * database. Chromium only.
 */
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 8081);
const API_PORT = Number(process.env.E2E_API_PORT ?? 8001);
const FAKE_PORT = Number(process.env.E2E_FAKE_PORT ?? 18080);

process.env.E2E_API_PORT = String(API_PORT);
process.env.E2E_FAKE_PORT = String(FAKE_PORT);
process.env.E2E_WEB_ORIGIN = `http://127.0.0.1:${WEB_PORT}`;

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.e2e.ts',
  // One shared test user (its email is baked into the web export), so the
  // tests must not overlap.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI
    ? [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]]
    : [['list']],
  outputDir: 'test-results',
  use: {
    baseURL: `http://127.0.0.1:${WEB_PORT}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    // Locale-stable strings: the suite reads the English copy.
    locale: 'en-US',
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        // Pre-installed browser in sandboxes without `playwright install`.
        launchOptions: process.env.E2E_CHROMIUM_PATH
          ? { executablePath: process.env.E2E_CHROMIUM_PATH }
          : {},
      },
    },
  ],
  webServer: [
    {
      command: 'python e2e/stack/run_backend.py',
      url: `http://127.0.0.1:${API_PORT}/health`,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command: `node e2e/stack/serve-web.mjs dist ${WEB_PORT}`,
      url: `http://127.0.0.1:${WEB_PORT}`,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
  ],
});
