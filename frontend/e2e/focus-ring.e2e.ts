import type { Page } from '@playwright/test';

import { expect, test } from './support/fixtures';
import { openTab } from './support/flows';

/** Computed outline of the element that has focus (#923). */
const focusedOutline = (page: Page, container = false) =>
  page.evaluate((onParent) => {
    const el = document.activeElement as Element;
    const s = getComputedStyle(onParent ? (el.parentElement as Element) : el);
    return { style: s.outlineStyle, width: parseFloat(s.outlineWidth) };
  }, container);

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
    expect((await focusedOutline(page, true)).style).toBe('solid');

    // Tab moves to the next control with a visible, at least 2px ring.
    await page.keyboard.press('Tab');
    await expect(search).not.toBeFocused();
    const tabbed = await focusedOutline(page);
    expect(tabbed.style).not.toBe('none');
    expect(tabbed.width).toBeGreaterThanOrEqual(2);

    // A pointer press on a chip focuses it without a ring.
    const chip = page.getByRole('button', { name: 'Filter by All' });
    await chip.click();
    await expect(chip).toBeFocused();
    expect((await focusedOutline(page)).style).toBe('none');
  });
});
