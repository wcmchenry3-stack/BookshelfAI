import { expect, test } from './support/fixtures';
import { openTab } from './support/flows';

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
    await openTab(page, 'Your book collection');

    // Library loads: every seeded book is listed.
    await expect(page.getByRole('button', { name: 'Dune — Purchased' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Emma — Wishlist' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'The Great Gatsby — Reading' })).toBeVisible();
    await expectNoA11yViolations('My Books with three books');

    // Status filter: a client-side view of the one list, so no new request.
    const listRequests: string[] = [];
    page.on('request', (req) => {
      if (req.method() === 'GET' && /\/user-books(\?|$)/.test(req.url()))
        listRequests.push(req.url());
    });
    await page.getByRole('button', { name: 'Filter by Purchased' }).click();
    await expect(page.getByRole('button', { name: 'Dune — Purchased' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Emma — Wishlist' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'The Great Gatsby — Reading' })).toHaveCount(0);
    await expectNoA11yViolations('My Books filtered to Purchased');
    await page.getByRole('button', { name: 'Filter by All' }).click();
    await expect(page.getByRole('button', { name: 'Emma — Wishlist' })).toBeVisible();
    expect(listRequests).toEqual([]);

    // Text filter.
    await page.getByRole('textbox', { name: 'Search your books' }).fill('austen');
    await expect(page.getByRole('button', { name: 'Emma — Wishlist' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Dune — Purchased' })).toHaveCount(0);
    await page.getByRole('textbox', { name: 'Search your books' }).fill('');
    await expect(page.getByRole('button', { name: 'Dune — Purchased' })).toBeVisible();

    // Details.
    await page.getByRole('button', { name: 'Dune — Purchased' }).click();
    const detail = page.getByRole('dialog');
    await expect(detail.getByRole('heading', { name: 'Dune' })).toBeVisible();
    await expect(detail.getByText('Frank Herbert')).toBeVisible();
    await expect(detail.getByText('A fake description of Dune.')).toBeVisible();
    await expect(detail.getByText('300 pages')).toBeVisible();
    await expectNoA11yViolations('My Books book detail');
    await detail.getByRole('button', { name: 'Close detail' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
  });

  test('moves a book to the next status from the row menu', async ({
    page,
    signIn,
    expectNoA11yViolations,
  }) => {
    await signIn();
    await openTab(page, 'Your book collection');
    await expect(page.getByRole('button', { name: 'Dune — Purchased' })).toBeVisible();

    await page.getByRole('button', { name: 'More options for Dune' }).click();
    const menu = page.getByRole('dialog');
    await expect(menu.getByRole('button', { name: 'View details' })).toBeVisible();
    await expect(menu.getByRole('button', { name: 'Remove from library' })).toBeVisible();
    await expectNoA11yViolations('My Books row menu');

    await menu.getByRole('button', { name: 'Mark as Reading' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Dune — Reading' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Dune — Purchased' })).toHaveCount(0);

    // It now belongs under the Reading filter, not Purchased.
    await page.getByRole('button', { name: 'Filter by Purchased' }).click();
    await expect(page.getByRole('button', { name: 'Dune — Reading' })).toHaveCount(0);
    await expectNoA11yViolations('My Books after moving a book');
  });

  test('shows the empty states for a status chip and for a search with no match', async ({
    page,
    signIn,
    expectNoA11yViolations,
  }) => {
    await signIn();
    await openTab(page, 'Your book collection');
    await expect(page.getByRole('button', { name: 'Dune — Purchased' })).toBeVisible();

    // A chip with no books: the search field and chips stay, and one tap returns to All.
    await page.getByRole('button', { name: /^Filter by Read,/ }).click();
    await expect(page.getByRole('heading', { name: 'Nothing marked read yet' })).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Search your books' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Filter by All' })).toBeVisible();
    await expectNoA11yViolations('My Books with an empty status filter');
    await page.getByRole('button', { name: 'Show all books' }).click();
    await expect(page.getByRole('button', { name: 'Dune — Purchased' })).toBeVisible();

    // A search with no match.
    await page.getByRole('textbox', { name: 'Search your books' }).fill('zzzz');
    await expect(page.getByRole('heading', { name: 'No books match “zzzz”' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Filter by All' })).toBeVisible();
    await expectNoA11yViolations('My Books with a search that matches nothing');
    await page.getByRole('button', { name: 'Clear search' }).first().click();
    await expect(page.getByRole('button', { name: 'Dune — Purchased' })).toBeVisible();
  });
});
