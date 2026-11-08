import { expect, KNOWN_VIOLATIONS, test } from './support/fixtures';
import { addFromPicker, searchBook } from './support/flows';

// The search goes through the real backend and the real Open Library and
// Google Books clients, pointed at the fake catalog in
// e2e/stack/fake_services.py (Google Books has no base-URL setting, so
// e2e/stack/run_backend.py rebinds the module constant). No public network.
test.describe('Add a book, delete it, restore it from Trash', () => {
  test('searches, adds, deletes and undoes the delete from Trash', async ({
    page,
    backend,
    signIn,
    expectNoA11yViolations,
  }) => {
    void backend;
    await signIn();

    // Search -> add.
    await searchBook(page, 'frankenstein', 'Frankenstein');
    await expectNoA11yViolations('search result picker', {
      known: [KNOWN_VIOLATIONS.pickerCheckboxMissingAriaChecked],
    });
    await addFromPicker(page, 'Frankenstein', 'Mary Shelley');
    await expect(page.getByRole('alert')).toContainText(
      '"Frankenstein" has been added to your wishlist.'
    );

    await page.getByRole('tab', { name: 'Your book collection' }).click();
    await expect(page.getByRole('button', { name: 'Frankenstein — Wishlist' })).toBeVisible();

    // Delete.
    await page.getByRole('button', { name: 'More options for Frankenstein' }).click();
    await page.getByRole('button', { name: 'Remove from library' }).click();
    await expect(page.getByRole('alert')).toContainText(
      'Moved to Trash. You can restore it within 30 days.'
    );
    await expect(page.getByRole('button', { name: 'Frankenstein — Wishlist' })).toHaveCount(0);
    await expect(page.getByText('No books here yet.')).toBeVisible();

    // Undo from Trash.
    await page.getByRole('button', { name: 'View trash' }).click();
    await expect(page).toHaveURL(/\/trash$/);
    await expect(page.getByRole('heading', { name: 'Trash' })).toBeVisible();
    // Earlier runs against the same database leave copies in Trash: newest first.
    await expect(page.getByText('Mary Shelley').first()).toBeVisible();
    // The days-left line (#932): a just-deleted book has about 30 days left.
    await expect(page.getByText(/Deleted for good in \d+ days?/).first()).toBeVisible();
    await expectNoA11yViolations('Trash with one book');
    await page.getByRole('button', { name: 'Restore Frankenstein' }).first().click();
    await expect(page.getByText('Restored Frankenstein to your library.')).toBeVisible();

    await page.getByRole('button', { name: 'Back', exact: true }).click();
    await page.getByRole('tab', { name: 'Your book collection' }).click();
    await expect(page.getByRole('button', { name: 'Frankenstein — Wishlist' })).toBeVisible();
    // No allowances: #907 removed the nested buttons and #898's success colour passes contrast.
    await expectNoA11yViolations('My Books after restoring');
  });
});
