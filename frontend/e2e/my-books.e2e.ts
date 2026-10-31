import { expect, KNOWN_VIOLATIONS, test } from './support/fixtures';

test.describe('My Books', () => {
  test.beforeEach(async ({ backend }) => {
    await backend.addBook('dune', 'purchased');
    await backend.addBook('emma', 'wishlisted');
    await backend.addBook('gatsby', 'reading');
  });

  test('loads the library, filters it and shows a book detail', async ({
    page,
    signIn,
    expectNoA11yViolations,
  }) => {
    await signIn();

    // Library loads: every seeded book is listed.
    await expect(page.getByRole('button', { name: 'Dune — purchased' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Emma — wishlisted' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'The Great Gatsby — reading' })).toBeVisible();
    await expectNoA11yViolations('My Books with three books', {
      known: [KNOWN_VIOLATIONS.bookCardNestedInteractive],
    });

    // Status filter.
    await page.getByRole('button', { name: 'Filter by Purchased' }).click();
    await expect(page.getByRole('button', { name: 'Dune — purchased' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Emma — wishlisted' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'The Great Gatsby — reading' })).toHaveCount(0);
    await expectNoA11yViolations('My Books filtered to Purchased', {
      known: [KNOWN_VIOLATIONS.bookCardNestedInteractive],
    });
    await page.getByRole('button', { name: 'Filter by All' }).click();
    await expect(page.getByRole('button', { name: 'Emma — wishlisted' })).toBeVisible();

    // Text filter.
    await page.getByRole('textbox', { name: 'Search your library' }).fill('austen');
    await expect(page.getByRole('button', { name: 'Emma — wishlisted' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Dune — purchased' })).toHaveCount(0);
    await page.getByRole('textbox', { name: 'Search your library' }).fill('');
    await expect(page.getByRole('button', { name: 'Dune — purchased' })).toBeVisible();

    // Details.
    await page.getByRole('button', { name: 'Dune — purchased' }).click();
    const detail = page.getByRole('dialog');
    await expect(detail.getByRole('heading', { name: 'Dune' })).toBeVisible();
    await expect(detail.getByText('Frank Herbert')).toBeVisible();
    await expect(detail.getByText('A fake description of Dune.')).toBeVisible();
    await expect(detail.getByText('300 pages')).toBeVisible();
    await expectNoA11yViolations('My Books book detail', {
      known: [KNOWN_VIOLATIONS.bookCardNestedInteractive],
    });
    await detail.getByRole('button', { name: 'Close detail' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
  });
});
