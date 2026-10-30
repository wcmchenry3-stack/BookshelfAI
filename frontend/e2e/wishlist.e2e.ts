import { expect, KNOWN_VIOLATIONS, test } from './support/fixtures';
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
    await expect(page.getByText(/Your wishlist is empty/)).toBeVisible();
    await expectNoA11yViolations('empty Wishlist');

    // Add from a search.
    await searchBook(page, 'emma', 'Emma');
    await addFromPicker(page, 'Emma', 'Jane Austen');

    await page.getByRole('tab', { name: 'Your wishlist' }).click();
    // Tabs stay mounted, so look for the Wishlist screen's own buttons.
    const removeButton = (title: string) =>
      page.getByRole('button', { name: `Remove ${title} from wishlist` });
    await expect(removeButton('Emma')).toBeVisible();
    await expect(removeButton('Dune')).toHaveCount(0); // purchased, not wishlisted
    await expectNoA11yViolations('Wishlist with one book', {
      known: [
        KNOWN_VIOLATIONS.wishlistCoverPlaceholderLabel,
        KNOWN_VIOLATIONS.successBannerContrast, // "added to your wishlist" banner
      ],
    });

    // Remove.
    await removeButton('Emma').click();
    await expect(removeButton('Emma')).toHaveCount(0);
    await expect(page.getByText(/Your wishlist is empty/)).toBeVisible();
  });
});
