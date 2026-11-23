import { expect, FAKE_URL, SHELF_PHOTO, test } from './support/fixtures';

// The backend's AI provider is the fake server from e2e/stack/fake_services.py
// (AI_ANTHROPIC_BASE_URL, scan.standard switched to the Anthropic adapter):
// every photo "contains" Dune and The Great Gatsby. TURNSTILE_REQUIRED=false.
test.describe('Scan upload', () => {
  test('uploads a photo, shows progress, then the results', async ({
    page,
    backend,
    signIn,
    expectNoA11yViolations,
  }) => {
    await backend.forgetScans();
    // #1023: start without AI consent, so the prompt is part of the flow.
    await backend.withdrawAiConsent();
    const callsBefore = await backend.providerCalls();
    await signIn();
    await page.getByRole('tab', { name: 'Scan a book' }).click();
    await expectNoA11yViolations('Scan tab');

    // Slow the answer a little so the progress state is observable.
    await page.route('**/scan', async (route) => {
      if (route.request().method() === 'POST') await new Promise((r) => setTimeout(r, 1000));
      await route.continue();
    });
    await page.locator('input[type=file]').setInputFiles({
      name: 'shelf.jpg',
      mimeType: 'image/jpeg',
      buffer: SHELF_PHOTO,
    });

    // #1023: the photo is not sent until the user agrees to AI scanning.
    await expect(page.getByRole('heading', { name: 'Scanning uses AI' })).toBeVisible();
    expect(await backend.providerCalls()).toBe(callsBefore);
    await expectNoA11yViolations('AI consent prompt');
    await page.getByRole('button', { name: 'Continue' }).click();

    const progress = page.getByRole('progressbar', {
      name: 'Reading your books and looking up matches…',
    });
    await expect(progress).toBeVisible();
    await expectNoA11yViolations('scan in progress');

    await expect(page.getByRole('alert')).toContainText('Found 2 books');
    await expect(progress).toHaveCount(0);
    await page.getByRole('button', { name: 'View' }).click();
    const picker = page.getByRole('dialog');
    await expect(
      picker.getByRole('checkbox', { name: 'Select Dune by Frank Herbert' })
    ).toBeVisible();
    await expect(
      picker.getByRole('checkbox', { name: 'Select The Great Gatsby by F. Scott Fitzgerald' })
    ).toBeVisible();
    await expectNoA11yViolations('scan results');
    expect(await backend.providerCalls()).toBe(callsBefore + 1);
  });

  test('POST /scan answers from the fake provider, never a real model', async ({ backend }) => {
    await backend.forgetScans();
    const callsBefore = await backend.providerCalls();

    const res = await backend.scan(SHELF_PHOTO);

    expect(res.status()).toBe(200);
    const body = await res.json();
    expect(body.books.map((b: { title: string }) => b.title).sort()).toEqual([
      'Dune',
      'The Great Gatsby',
    ]);
    expect(await backend.providerCalls()).toBe(callsBefore + 1);
    // The count is the fake's own: it listens on loopback only.
    expect(FAKE_URL).toMatch(/^http:\/\/127\.0\.0\.1:/);
  });
});
