import { expect, test } from './support/fixtures';

test.describe('Auth', () => {
  test('signs in with the dev test login and lands on Scan', async ({
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

    await expect(page).toHaveURL(/\/scan$/);
    await expect(page.getByRole('tab', { name: 'Scan a book' })).toHaveAttribute(
      'aria-selected',
      'true'
    );
    await expect(page.getByRole('button', { name: 'Text search mode' })).toBeVisible();
    await expectNoA11yViolations('Scan after sign-in');
  });
});
