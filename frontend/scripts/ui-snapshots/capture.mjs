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
// --video records one walkthrough per theme (<out>/<theme>-walkthrough.mp4),
// holding on each screen and showing taps. It uses Chromium's screencast (no
// Playwright ffmpeg download needed) and needs ffmpeg on PATH to encode.
// --themes light,dark (default both) limits the run.
//
// The dev server is required (not a production export): sign-in uses the
// __DEV__-only "Test Login" button, intercepted below. Web keeps tokens in
// memory only, so the run signs in once per theme and then navigates by tapping,
// never by reloading.

import { spawnSync } from 'node:child_process';
import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, sep } from 'node:path';
import { createRequire } from 'node:module';
import { Buffer } from 'node:buffer';
import { URL } from 'node:url';

import { coverSvg, searchResults, trash, usage, userBooks } from './fixtures.mjs';

const require = createRequire(import.meta.url);
// playwright-core is a declared devDependency (the `playwright` package is not).
const { chromium } = require('playwright-core');

const args = process.argv.slice(2);
function exitWithUsage(message) {
  console.error(`capture.mjs: ${message}`);
  process.exit(2);
}
const arg = (name, fallback) => {
  const i = args.indexOf(name);
  if (i < 0) return fallback;
  const value = args[i + 1];
  if (value === undefined || value.startsWith('--')) exitWithUsage(`${name} needs a value`);
  return value;
};
const APP_URL = arg('--url', 'http://localhost:8081');
const OUT = resolve(arg('--out', 'ui-snapshots/current'));
// The run deletes <out>/<theme>/ before capturing, so --out must be a
// dedicated folder below the current one, and --themes a known theme name.
if (!OUT.startsWith(process.cwd() + sep) && !OUT.startsWith(tmpdir() + sep)) {
  exitWithUsage(`--out must be a folder inside ${process.cwd()} or the temp folder (got ${OUT})`);
}
if (!['localhost', '127.0.0.1'].includes(new URL(APP_URL).hostname)) {
  exitWithUsage(`--url must be a local dev server (got ${APP_URL})`);
}
const API = 'http://mock.api';
const VIEWPORT = { width: 390, height: 844 };
const VIDEO = args.includes('--video');
const THEMES = arg('--themes', 'light,dark').split(',');
for (const t of THEMES) if (!['light', 'dark'].includes(t)) exitWithUsage(`unknown theme "${t}"`);
// How long a recording holds on each captured screen.
const HOLD_MS = VIDEO ? 1400 : 0;

// Draws a fading dot wherever the run taps, so a recording shows the flow.
const TAP_INDICATOR = `
  window.addEventListener('pointerdown', (e) => {
    const d = document.createElement('div');
    d.style.cssText = 'position:fixed;z-index:2147483647;pointer-events:none;width:36px;height:36px;' +
      'margin:-18px 0 0 -18px;border-radius:18px;background:rgba(15,66,111,.35);' +
      'border:2px solid rgba(255,255,255,.9);transition:opacity .5s, transform .5s;';
    d.style.left = e.clientX + 'px';
    d.style.top = e.clientY + 'px';
    document.documentElement.appendChild(d);
    requestAnimationFrame(() => { d.style.opacity = '0'; d.style.transform = 'scale(1.6)'; });
    setTimeout(() => d.remove(), 600);
  }, true);
`;

const emptyLists = { value: false };
const consoleErrors = new Set();
const failedSteps = [];
const blockedOrigins = new Set();

async function mockApi(page) {
  // Everything that is neither the local dev server nor the mock API is
  // blocked, so a stray EXPO_PUBLIC_API_URL or Sentry DSN can never reach a
  // real service. Routes registered later take precedence, so the mock below
  // still answers http://mock.api.
  const appOrigin = new URL(APP_URL).origin;
  await page.route('**/*', (route) => {
    const url = route.request().url();
    if (url.startsWith(appOrigin + '/')) return route.continue();
    blockedOrigins.add(new URL(url).origin);
    return route.abort('blockedbyclient');
  });
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
  if (HOLD_MS) await page.waitForTimeout(HOLD_MS);
}

const byLabel = (page, label) => page.locator(`[aria-label="${label}"]`).first();

async function tab(page, label) {
  await page.locator(`[role="tab"][aria-label="${label}"]`).click();
  await settle(page);
}

// Scrolls the tallest scrollable element (the screen's list); the mouse wheel
// does nothing under touch emulation.
async function scrollList(page, dy) {
  /* global document, getComputedStyle -- runs in the page via page.evaluate */
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
    const reason = err.message.split('\n')[0];
    failedSteps.push(`${name}: ${reason}`);
    console.warn(`  ✗ ${name}: ${reason}`);
  }
}

async function runTheme(browser, scheme) {
  const dir = join(OUT, scheme);
  // Start each theme from an empty folder: a step that fails must leave no
  // stale screenshot from an earlier run looking like a current result.
  rmSync(dir, { recursive: true, force: true });
  rmSync(join(OUT, `${scheme}-walkthrough.mp4`), { force: true });
  mkdirSync(dir, { recursive: true });
  console.log(`\n${scheme}`);
  const context = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,
    colorScheme: scheme,
    isMobile: true,
    hasTouch: true,
  });
  if (VIDEO) await context.addInitScript(TAP_INDICATOR);
  const page = await context.newPage();
  const recorder = VIDEO ? await startRecording(context, page, scheme) : null;
  try {
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
    await shot(page, dir, '01b-landing-after-sign-in');
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
  } finally {
    if (recorder) await recorder.stop();
    await context.close();
  }
}

// Escapes a path for an ffmpeg concat list entry ('...' quoting).
const quote = (path) => path.replace(/'/g, "'\\''");

// Records the page with Chromium's screencast: each frame is saved with its
// timestamp, then ffmpeg turns them into a constant-30fps MP4.
async function startRecording(context, page, scheme) {
  const frameDir = join(OUT, `${scheme}-frames`);
  rmSync(frameDir, { recursive: true, force: true });
  mkdirSync(frameDir, { recursive: true });
  const cdp = await context.newCDPSession(page);
  const frames = [];
  cdp.on('Page.screencastFrame', async ({ data, metadata, sessionId }) => {
    const file = join(frameDir, `${String(frames.length).padStart(6, '0')}.jpg`);
    writeFileSync(file, Buffer.from(data, 'base64'));
    frames.push({ file, t: metadata.timestamp });
    await cdp.send('Page.screencastFrameAck', { sessionId }).catch(() => {});
  });
  await cdp.send('Page.startScreencast', {
    format: 'jpeg',
    quality: 85,
    maxWidth: VIEWPORT.width * 2,
    maxHeight: VIEWPORT.height * 2,
  });
  return {
    async stop() {
      await cdp.send('Page.stopScreencast').catch(() => {});
      // Hold the last frame for a moment so the video doesn't end abruptly.
      const end = (frames.at(-1)?.t ?? 0) + 1;
      const lines = frames.map((f, i) => {
        const next = frames[i + 1]?.t ?? end;
        return `file '${quote(f.file)}'\nduration ${Math.max(0.001, next - f.t).toFixed(3)}`;
      });
      if (frames.length) lines.push(`file '${quote(frames.at(-1).file)}'`);
      const list = join(frameDir, 'frames.txt');
      writeFileSync(list, lines.join('\n') + '\n');
      const mp4 = join(OUT, `${scheme}-walkthrough.mp4`);
      const r = spawnSync(
        'ffmpeg',
        [
          '-y',
          '-loglevel',
          'error',
          '-f',
          'concat',
          '-safe',
          '0',
          '-i',
          list,
          '-vf',
          'scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30',
          '-c:v',
          'libx264',
          '-pix_fmt',
          'yuv420p',
          '-movflags',
          '+faststart',
          mp4,
        ],
        { stdio: 'inherit' }
      );
      if (r.status === 0) {
        rmSync(frameDir, { recursive: true, force: true });
        console.log(`  ▶ ${mp4}`);
      } else {
        const why = r.error?.message ?? `ffmpeg exited ${r.status}`;
        failedSteps.push(`${scheme} video: ${why} (frames kept in ${frameDir})`);
        console.warn(`  ✗ video: ${why}; frames kept in ${frameDir}`);
      }
    },
  };
}

// CHROMIUM_PATH: use a pre-installed browser when the pinned Playwright's own
// download is absent (e.g. cloud sessions: /opt/pw-browsers/chromium).
mkdirSync(OUT, { recursive: true });
rmSync(join(OUT, 'console-errors.txt'), { force: true });
const browser = await chromium.launch(
  process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}
);
try {
  for (const scheme of THEMES) {
    // A failure outside a step (e.g. sign-in) skips the rest of that theme
    // but is reported, and the other themes still run.
    await runTheme(browser, scheme).catch((err) => {
      const reason = err.message.split('\n')[0];
      failedSteps.push(`${scheme}: ${reason}`);
      console.warn(`  ✗ ${scheme}: ${reason}`);
    });
  }
} finally {
  await browser.close();
  writeFileSync(join(OUT, 'console-errors.txt'), [...consoleErrors].join('\n') + '\n');
}
console.log(`\nSaved to ${OUT} (${consoleErrors.size} distinct console errors)`);
if (blockedOrigins.size) console.log(`Blocked requests to: ${[...blockedOrigins].join(', ')}`);
if (failedSteps.length) {
  // A partial capture must not pass for a complete one in a before/after.
  console.error(`\n${failedSteps.length} step(s) failed; their screenshots are missing:`);
  for (const f of failedSteps) console.error(`  - ${f}`);
  process.exitCode = 1;
}
