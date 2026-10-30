import { expect, test } from './support/fixtures';

test.describe('Auth', () => {
  test('signs in with the dev test login and lands on My Books', async ({
    page,
    backend,
    signIn,
    expectNoA11yViolations,
  }) => {
    void backend; // starts every test from an empty library

    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'BookshelfAI' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Sign in with Google' })).toBeVisible();
    await expectNoA11yViolations('login screen');

    await signIn();

    await expect(page).toHaveURL(/\/my-books$/);
    await expect(page.getByRole('tab', { name: 'Your book collection' })).toHaveAttribute(
      'aria-selected',
      'true'
    );
    await expect(page.getByRole('textbox', { name: 'Search your library' })).toBeVisible();
    await expect(page.getByText('No books here yet.')).toBeVisible();
    await expectNoA11yViolations('My Books after sign-in');
  });
});
