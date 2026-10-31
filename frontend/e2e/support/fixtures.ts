import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import AxeBuilder from '@axe-core/playwright';
import { test as base, expect, type APIRequestContext } from '@playwright/test';

/**
 * Shared fixtures for the web e2e suite (#470).
 *
 * The values below must match what `npm run e2e:build` bakes into the web
 * export (EXPO_PUBLIC_TEST_AUTH_SECRET / EXPO_PUBLIC_TEST_EMAIL) and what
 * `e2e/stack/run_backend.py` listens on.
 */
const API_URL = `http://127.0.0.1:${process.env.E2E_API_PORT ?? 8001}`;
export const FAKE_URL = `http://127.0.0.1:${process.env.E2E_FAKE_PORT ?? 18080}`;
const TEST_EMAIL = process.env.E2E_TEST_EMAIL ?? 'e2e@example.test';
const TEST_SECRET = process.env.E2E_TEST_AUTH_SECRET ?? 'e2e-secret';

/** WCAG 2.2 AA, the standard the app targets (docs/claude/frontend.md). */
const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'];

/**
 * A real accessibility defect that exists in the app today. Only the exact
 * rule on the matching nodes is tolerated (every other rule still runs on
 * them), and a call that lists one fails once axe stops reporting it.
 * Never add one without an open issue.
 */
export interface KnownViolation {
  rule: string;
  /** Matches the offending node's HTML (its opening tag, as axe reports it). */
  html: RegExp;
  /** Also matched against axe's explanation of the failure, when set. */
  message?: RegExp;
  /** On screen only for a moment (a banner): not an error when axe misses it. */
  transient?: boolean;
  issue: string;
}

export const KNOWN_VIOLATIONS = {
  // Scan screen: the progress row's ActivityIndicator renders as a nameless
  // role="progressbar" next to the labelled progress row.
  scanSpinnerNoName: {
    rule: 'aria-progressbar-name',
    html: /^<div role="progressbar"/,
    transient: true,
    issue: 'scan ActivityIndicator is a progressbar with no name (aria-progressbar-name, serious)',
  },
  // InAppBanner, success type: white text on the #16a34a success colour.
  successBannerContrast: {
    rule: 'color-contrast',
    html: /./,
    message: /background color: #16a34a/,
    transient: true,
    issue: 'success banner text is 3.29:1 on #16a34a, needs 4.5:1 (color-contrast, serious)',
  },
  // Wishlist cover placeholder: a <div aria-label="No cover available">
  // with no role.
  wishlistCoverPlaceholderLabel: {
    rule: 'aria-prohibited-attr',
    html: /aria-label="No cover available"/,
    issue: 'cover placeholder has aria-label on a role-less div (aria-prohibited-attr, serious)',
  },
  // BookCandidatePicker rows have role="checkbox" without aria-checked.
  pickerCheckboxMissingAriaChecked: {
    rule: 'aria-required-attr',
    html: /^<[a-z]+ [^>]*role="checkbox"/,
    issue: 'picker row has role=checkbox without aria-checked (aria-required-attr, critical)',
  },
  // Book cards (My Books, Wishlist) are a button that contains the
  // "Mark as ..." and "Remove" buttons.
  bookCardNestedInteractive: {
    rule: 'nested-interactive',
    html: /^<[a-z]+ [^>]*aria-label="[^"]+ — [^"]+"/,
    issue: 'book card is a button containing buttons (nested-interactive, serious)',
  },
} satisfies Record<string, KnownViolation>;

type LibraryStatus = 'wishlisted' | 'purchased' | 'reading' | 'read';

/** Calls the backend directly as the e2e test user (seeding and clean-up). */
class Backend {
  private token: string | null = null;

  constructor(private readonly request: APIRequestContext) {}

  private async headers(): Promise<Record<string, string>> {
    if (!this.token) {
      const res = await this.request.post(`${API_URL}/auth/test-login`, {
        data: { secret: TEST_SECRET, email: TEST_EMAIL },
      });
      expect(
        res.ok(),
        'POST /auth/test-login (is the backend running with TEST_AUTH_SECRET?)'
      ).toBe(true);
      this.token = (await res.json()).access_token as string;
    }
    return { Authorization: `Bearer ${this.token}` };
  }

  /** Soft-deletes every library entry, so each test starts with an empty library. */
  async resetLibrary(): Promise<void> {
    const headers = await this.headers();
    const list = await this.request.get(`${API_URL}/user-books`, { headers });
    expect(list.ok()).toBe(true);
    for (const entry of (await list.json()) as { id: string }[]) {
      const res = await this.request.delete(`${API_URL}/user-books/${entry.id}`, { headers });
      expect(res.status()).toBe(204);
    }
  }

  /** Adds the first catalog hit for `query` to the library, as the app does
   * (search, then POST /wishlist with the signed result). */
  async addBook(query: string, status: LibraryStatus = 'wishlisted'): Promise<void> {
    const headers = await this.headers();
    const search = await this.request.get(`${API_URL}/books/search`, {
      headers,
      params: { q: query },
    });
    expect(search.ok(), `GET /books/search?q=${query}`).toBe(true);
    const [hit] = await search.json();
    expect(hit, `the fake catalog has a book for "${query}"`).toBeTruthy();
    const added = await this.request.post(`${API_URL}/wishlist`, { headers, data: hit });
    expect(added.status()).toBe(201);
    if (status !== 'wishlisted') {
      const { id } = await added.json();
      const patched = await this.request.patch(`${API_URL}/user-books/${id}`, {
        headers,
        data: { status },
      });
      expect(patched.ok()).toBe(true);
    }
  }

  /** `POST /scan` with a photo, as the app would send it. */
  async scan(image: Buffer) {
    return this.request.post(`${API_URL}/scan`, {
      headers: await this.headers(),
      multipart: { file: { name: 'shelf.jpg', mimeType: 'image/jpeg', buffer: image } },
    });
  }

  /** Clears every saved scan record (the e2e database only), so the same
   * photo is read by the provider again instead of being answered from the
   * record of an earlier run. */
  async forgetScans(): Promise<void> {
    const res = await this.request.post(`${FAKE_URL}/__forget_scans`);
    expect(res.ok(), 'POST /__forget_scans').toBe(true);
  }

  /** How many calls the fake AI provider has received. */
  async providerCalls(): Promise<number> {
    const res = await this.request.get(`${FAKE_URL}/__count`);
    return (await res.json()).calls as number;
  }
}

type Fixtures = {
  backend: Backend;
  /** Signs in through the login screen's dev "Test Login" button. */
  signIn: () => Promise<void>;
  /** Fails the test on any WCAG 2.2 AA violation on the current page. */
  expectNoA11yViolations: (
    context: string,
    options?: { known?: KnownViolation[] }
  ) => Promise<void>;
};

export const test = base.extend<Fixtures>({
  page: async ({ page }, provide) => {
    // The e2e export is a development bundle (the only build that shows the
    // "Test Login" button, `__DEV__`). Its React Native LogBox renders a
    // fixed `#error-toast` over the tab bar that swallows clicks and is not
    // part of the app's UI: hide it. Real problems it reports (such as
    // invalid nested buttons) are still caught by the axe checks.
    await page.addInitScript(() => {
      document.addEventListener('DOMContentLoaded', () => {
        const style = document.createElement('style');
        style.textContent = '#error-toast { display: none !important; }';
        document.head.appendChild(style);
      });
    });
    await provide(page);
  },

  backend: async ({ playwright }, provide) => {
    const request = await playwright.request.newContext();
    const backend = new Backend(request);
    await backend.resetLibrary();
    await provide(backend);
    await request.dispose();
  },

  signIn: async ({ page }, provide) => {
    await provide(async () => {
      await page.goto('/');
      await page.getByRole('button', { name: 'Test Login' }).click();
      await expect(page.getByRole('tab', { name: 'Your book collection' })).toBeVisible();
    });
  },

  expectNoA11yViolations: async ({ page }, provide) => {
    await provide(async (context: string, options: { known?: KnownViolation[] } = {}) => {
      // Let fades and slides finish: axe reads colours mid-transition.
      await page.waitForTimeout(500);
      const { violations } = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();
      const known = options.known ?? [];
      const seen = new Set<KnownViolation>();
      const unexpected: string[] = [];
      for (const violation of violations) {
        for (const node of violation.nodes) {
          const target = node.target.join(' ');
          const explanation = node.any.map((check) => check.message).join('; ');
          const match = known.find(
            (k) =>
              k.rule === violation.id &&
              k.html.test(node.html) &&
              (!k.message || k.message.test(explanation))
          );
          if (match) seen.add(match);
          else
            unexpected.push(
              `${violation.id} (${violation.impact}): ${violation.help} -> ${target} :: ${node.html.slice(0, 160)} :: ${node.any
                .map((c) => c.message)
                .join('; ')
                .slice(0, 200)}`
            );
        }
      }
      expect(unexpected, `axe WCAG 2.2 AA violations: ${context}`).toEqual([]);
      // A known violation that is gone means the app was fixed: remove it
      // from KNOWN_VIOLATIONS and from this call.
      for (const k of known.filter((k) => !k.transient)) {
        expect(seen.has(k), `known violation fixed, remove it from the test: ${k.issue}`).toBe(
          true
        );
      }
    });
  },
});

export { expect };

/** A small photo of random pixels. The fake provider never looks at it. A
 * photo the server has seen before is answered from its scan record and never
 * reaches the provider (#585), so scan tests call `backend.forgetScans()`
 * first. */
export const SHELF_PHOTO = readFileSync(join(__dirname, 'shelf.jpg'));
