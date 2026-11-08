import { expect, test } from './support/fixtures';
import { openTab } from './support/flows';

test.describe('Keyboard focus ring', () => {
  test.beforeEach(async ({ backend }) => {
    await backend.addBook('dune', 'purchased');
  });

  test('shows a ring on Tab focus and none after a pointer press', async ({ page, signIn }) => {
    await signIn();
    await openTab(page, 'Your book collection');
    await expect(page.getByRole('button', { name: 'Dune — Purchased' })).toBeVisible();

    // The search field shows its ring on any focus, click included. The ring is on the
    // field container (the input's parent), not on the input itself.
    const search = page.getByRole('textbox', { name: 'Search your books' });
    await search.click();
    await expect(search).toBeFocused();
    await expect(search.locator('..')).toHaveCSS('outline-style', 'solid');

    // With an empty query there is no clear button, so Tab moves to the "All" chip,
    // which shows a 2px ring.
    const all = page.getByRole('button', { name: 'Filter by All' });
    await page.keyboard.press('Tab');
    await expect(all).toBeFocused();
    await expect(all).toHaveCSS('outline-style', 'solid');
    await expect(all).toHaveCSS('outline-width', '2px');

    // A pointer press on a different chip moves focus there without a ring.
    const purchased = page.getByRole('button', { name: 'Filter by Purchased' });
    await purchased.click();
    await expect(purchased).toBeFocused();
    await expect(purchased).toHaveCSS('outline-style', 'none');
    await expect(all).toHaveCSS('outline-style', 'none');
  });
});
