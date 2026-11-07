#!/usr/bin/env node
// UI snapshot run (UX overhaul, Phase 0): drives the Expo web build at phone
// size against a mocked API and saves a screenshot of every screen and the
// main states, in light and dark. Used for before/after comparison on UI PRs.
//
// Usage (from frontend/):
//   EXPO_PUBLIC_API_URL=http://mock.api EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID=snapshot \
//     npx expo start --web --port 8081
//   node scripts/ui-snapshots/capture.mjs [--url http://localhost:8081] [--out ui-snapshots/current]
//   (set CHROMIUM_PATH to use an already-installed Chromium)
//
// The dev server is required (not a production export): sign-in uses the
// __DEV__-only "Test Login" button, intercepted below. Web keeps tokens in
// memory only, so the run signs in once per theme and then navigates by tapping,
// never by reloading.

import { mkdirSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { createRequire } from 'node:module';

import { coverSvg, searchResults, trash, usage, userBooks } from './fixtures.mjs';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');

const args = process.argv.slice(2);
const arg = (name, fallback) => {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : fallback;
};
const APP_URL = arg('--url', 'http://localhost:8081');
const OUT = resolve(arg('--out', 'ui-snapshots/current'));
const API = 'http://mock.api';
const VIEWPORT = { width: 390, height: 844 };

const emptyLists = { value: false };
const consoleErrors = new Set();

async function mockApi(page) {
  await page.route(`${API}/**`, async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const path = url.pathname;
    const method = req.method();
    const json = (body, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

    const cover = path.match(/^\/covers\/(\d+)\.svg$/);
    if (cover) return route.fulfill({ contentType: 'image/svg+xml', body: coverSvg(+cover[1]) });
    if (path === '/auth/test-login')
      return json({ access_token: 'snapshot-access', refresh_token: 'snapshot-refresh' });
    if (path === '/auth/me') return json({ id: 'u-1', email: 'reader@example.com' });
    if (path === '/scan/usage') return json(usage);
    if (path === '/user-books/trash') return json(trash(API));
    if (path === '/user-books' && method === 'GET') {
      if (emptyLists.value) return json([]);
      const status = url.searchParams.get('status');
      const all = userBooks(API);
      return json(status ? all.filter((b) => b.status === status) : all);
    }
    if (path === '/books/search') return json(searchResults(API));
    if (method === 'PATCH' || method === 'DELETE' || method === 'POST') return json({});
    console.warn(`  [mock] unhandled ${method} ${path}`);
    return json({ detail: 'not mocked' }, 404);
  });
}

async function settle(page, ms = 700) {
  await page.waitForLoadState('networkidle').catch(() => {});
  await page.waitForTimeout(ms);
}

async function shot(page, dir, name) {
  const file = join(dir, `${name}.png`);
  await page.screenshot({ path: file });
  console.log(`  ✓ ${name}`);
}

const byLabel = (page, label) => page.locator(`[aria-label="${label}"]`).first();

async function tab(page, label) {
  await page.locator(`[role="tab"][aria-label="${label}"]`).click();
  await settle(page);
}

// Scrolls the tallest scrollable element (the screen's list); the mouse wheel
// does nothing under touch emulation.
async function scrollList(page, dy) {
  await page.evaluate((delta) => {
    const els = [...document.querySelectorAll('div')].filter(
      (e) => e.scrollHeight > e.clientHeight + 20 && getComputedStyle(e).overflowY !== 'visible'
    );
    els.sort((a, b) => b.clientHeight - a.clientHeight);
    els[0]?.scrollBy(0, delta);
  }, dy);
  await settle(page, 400);
}

async function step(name, fn) {
  try {
    await fn();
  } catch (err) {
    console.warn(`  ✗ ${name}: ${err.message.split('\n')[0]}`);
  }
}

async function runTheme(browser, scheme) {
  const dir = join(OUT, scheme);
  mkdirSync(dir, { recursive: true });
  console.log(`\n${scheme}`);
  const context = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,
    colorScheme: scheme,
    isMobile: true,
    hasTouch: true,
  });
  const page = await context.newPage();
  page.on('pageerror', (e) => consoleErrors.add(`[pageerror] ${e.message.split('\n')[0]}`));
  page.on('console', (m) => {
    if (m.type() === 'error') consoleErrors.add(m.text().split('\n')[0].slice(0, 300));
  });
  await mockApi(page);
  emptyLists.value = false;

  await page.goto(APP_URL);
  // The dev server's error toast would cover the tab bar; errors are written
  // to console-errors.txt instead.
  await page.addStyleTag({ content: '#error-toast { display: none !important; }' });
  await settle(page, 2500);
  await shot(page, dir, '01-login');

  await byLabel(page, 'Test Login').click();
  await settle(page, 1500);
  await shot(page, dir, '00-landing-after-sign-in');
  await tab(page, 'Scan a book');
  await shot(page, dir, '02-scan-camera');

  await step('scan search', async () => {
    await byLabel(page, 'Text search mode').click();
    await settle(page, 300);
    await shot(page, dir, '03-scan-search');
    await byLabel(page, 'Book title or author search').fill('austen');
    await byLabel(page, 'Search for book').click();
    await settle(page, 1500);
    // The result arrives as a "Book found" banner; View opens the review picker.
    await shot(page, dir, '04-scan-result-banner');
    await byLabel(page, 'View').click();
    await settle(page, 800);
    await shot(page, dir, '04b-scan-review-picker');
    await byLabel(page, 'Close picker').click();
    await settle(page, 500);
    await byLabel(page, 'Camera scan mode').click();
  });

  await step('wishlist', async () => {
    await tab(page, 'Your wishlist');
    await shot(page, dir, '05-wishlist');
  });

  await step('my books', async () => {
    await tab(page, 'Your book collection');
    await shot(page, dir, '06-my-books');
    await scrollList(page, 700);
    await shot(page, dir, '07-my-books-scrolled');
    await scrollList(page, -5000);
    await byLabel(page, 'Filter by Reading').click();
    await settle(page);
    await shot(page, dir, '08-my-books-filter-reading');
    await byLabel(page, 'Filter by All').click();
    await settle(page);
    await page.locator('[aria-label^="Moby-Dick"]').first().click();
    await settle(page, 800);
    await shot(page, dir, '09-book-detail-sheet');
    const close = page.locator('[aria-label="Close detail"]').first();
    if (await close.isVisible().catch(() => false)) await close.click();
    else await page.keyboard.press('Escape');
    await settle(page, 500);
  });

  await step('settings', async () => {
    await tab(page, 'App settings');
    await shot(page, dir, '10-settings');
  });

  await step('feedback', async () => {
    await page.locator('[data-testid="settings-feedback"]').click();
    await settle(page, 800);
    await shot(page, dir, '11-feedback-sheet');
    await page.keyboard.press('Escape');
    await settle(page, 500);
  });

  await step('trash', async () => {
    await page.locator('[data-testid="settings-trash"]').click();
    await settle(page);
    await shot(page, dir, '12-trash');
    await page.goBack();
    await settle(page);
  });

  await step('empty states', async () => {
    emptyLists.value = true;
    await tab(page, 'Your book collection');
    await tab(page, 'Your wishlist');
    await shot(page, dir, '13-wishlist-empty');
    await tab(page, 'Your book collection');
    await shot(page, dir, '14-my-books-empty');
  });

  await context.close();
}

// CHROMIUM_PATH: use a pre-installed browser when the pinned Playwright's own
// download is absent (e.g. cloud sessions: /opt/pw-browsers/chromium).
const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}
);
try {
  for (const scheme of ['light', 'dark']) await runTheme(browser, scheme);
} finally {
  await browser.close();
}
writeFileSync(join(OUT, 'console-errors.txt'), [...consoleErrors].join('\n') + '\n');
console.log(`\nSaved to ${OUT} (${consoleErrors.size} distinct console errors)`);
