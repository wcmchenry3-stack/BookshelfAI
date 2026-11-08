import { expect, test } from './support/fixtures';
import { addFromPicker, searchBook } from './support/flows';

test.describe('Wishlist', () => {
  test('adds a book to the wishlist and removes it', async ({
    page,
    backend,
    signIn,
    expectNoA11yViolations,
  }) => {
    await backend.addBook('dune', 'purchased'); // not on the wishlist
    await signIn();

    await page.getByRole('tab', { name: 'Your wishlist' }).click();
    await expect(page.getByText('Nothing on your wishlist yet')).toBeVisible();
    await expectNoA11yViolations('empty Wishlist');

    // Add from a search.
    await searchBook(page, 'emma', 'Emma');
    await addFromPicker(page, 'Emma', 'Jane Austen');

    await page.getByRole('tab', { name: 'Your wishlist' }).click();
    // Tabs stay mounted: scope to the Wishlist screen, not My Books' rows.
    const screen = page.getByTestId('wishlist-screen');
    const row = (title: string) => screen.getByRole('button', { name: `${title} — Wishlist` });
    await expect(row('Emma')).toBeVisible();
    await expect(row('Dune')).toHaveCount(0); // purchased, not wishlisted
    await expectNoA11yViolations('Wishlist with one book');

    // Remove: tap the row, then use Remove in the detail sheet.
    await row('Emma').click();
    const detail = page.getByRole('dialog');
    await expect(detail.getByRole('heading', { name: 'Emma' })).toBeVisible();
    await expectNoA11yViolations('Wishlist book detail');
    await detail.getByRole('button', { name: 'Remove Emma' }).click();
    await expect(row('Emma')).toHaveCount(0);
    await expect(page.getByText('Nothing on your wishlist yet')).toBeVisible();
  });

  test('moves a book to purchased with the Bought chip', async ({
    page,
    backend,
    signIn,
    expectNoA11yViolations,
  }) => {
    await backend.addBook('emma', 'wishlisted');
    await signIn();

    await page.getByRole('tab', { name: 'Your wishlist' }).click();
    const bought = page.getByTestId('wishlist-screen').getByRole('button', {
      name: 'Mark Emma as bought',
    });
    await expect(bought).toBeVisible();
    await expect(bought).toContainText('Bought');
    await expectNoA11yViolations('Wishlist with a Bought chip');

    await bought.click();
    await expect(bought).toHaveCount(0);
    await expect(page.getByText('Nothing on your wishlist yet')).toBeVisible();
  });

  test('"Search by title" on an empty wishlist opens Scan in text search', async ({
    page,
    signIn,
  }) => {
    await signIn();

    await page.getByRole('tab', { name: 'Your wishlist' }).click();
    await expect(page.getByRole('heading', { name: 'Wishlist', exact: true })).toBeVisible();
    await expect(page.getByText('Nothing on your wishlist yet')).toBeVisible();

    await page.getByRole('button', { name: 'Search by title' }).click();
    await expect(page.getByLabel('Book title or author search')).toBeVisible();

    // The Scan tab stays mounted: "Scan books" must still open the camera, not text search.
    await page.getByRole('tab', { name: 'Your wishlist' }).click();
    await page.getByRole('button', { name: 'Scan books' }).click();
    // Camera mode: the text field is gone and the camera's "Scan Books" action shows.
    // (react-native-web does not emit aria-selected for role=button, so assert what renders.)
    await expect(page.getByLabel('Book title or author search')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Scan Books', exact: true })).toBeVisible();
  });
});
