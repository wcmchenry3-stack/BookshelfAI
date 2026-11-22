import { expect, type Page } from '@playwright/test';

/**
 * Open a tab by its accessible name (sign-in lands on Scan, so specs open the
 * tab they test): 'Scan a book', 'Your wishlist', 'Your book collection',
 * 'App settings'.
 */
export async function openTab(page: Page, name: string): Promise<void> {
  await page.getByRole('tab', { name }).click();
  await expect(page.getByRole('tab', { name })).toHaveAttribute('aria-selected', 'true');
}

/** Scan tab, Search mode: look up `query` and open the result picker. */
export async function searchBook(page: Page, query: string, title: string): Promise<void> {
  await page.getByRole('tab', { name: 'Scan a book' }).click();
  await page.getByRole('button', { name: 'Text search mode' }).click();
  await page.getByRole('textbox', { name: 'Book title or author search' }).fill(query);
  await page.getByRole('button', { name: 'Search for book' }).click();
  await expect(page.getByRole('alert')).toContainText(`Book found: ${title}`);
  await page.getByRole('button', { name: 'View' }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
}

/** With the picker open: select `title` and add it to the wishlist. */
export async function addFromPicker(page: Page, title: string, author: string): Promise<void> {
  const picker = page.getByRole('dialog');
  await picker.getByRole('checkbox', { name: `Select ${title} by ${author}` }).click();
  await picker.getByRole('button', { name: 'Add 1 book to wishlist' }).click();
  await expect(picker).toHaveCount(0);
}
