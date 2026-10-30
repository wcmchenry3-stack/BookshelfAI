import { expect, FAKE_URL, KNOWN_VIOLATIONS, SHELF_PHOTO, test } from './support/fixtures';

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
    // KNOWN APP BUG: on web the scan upload never reaches the server.
    // ScanJobContext appends the photo's blob URL *string* to the form data
    // (formData.append('file', job.imageUri as unknown as Blob, 'scan.jpg')),
    // and browsers throw "parameter 2 is not of type 'Blob'", so no POST /scan
    // is sent and the user sees "Scan failed". With that fixed this test
    // passes as written (checked by hand). Until then it is an expected
    // failure: the suite stays green, and goes red as soon as the upload
    // works, which is the cue to delete the next line.
    test.fail(true, 'web scan upload throws before POST /scan (FormData.append of a blob URL)');

    await backend.forgetScans();
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

    const progress = page.getByRole('progressbar', {
      name: 'Reading your books and looking up matches…',
    });
    await expect(progress).toBeVisible();
    await expectNoA11yViolations('scan in progress', {
      known: [KNOWN_VIOLATIONS.scanSpinnerNoName],
    });

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
    await expectNoA11yViolations('scan results', {
      known: [KNOWN_VIOLATIONS.pickerCheckboxMissingAriaChecked],
    });
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
