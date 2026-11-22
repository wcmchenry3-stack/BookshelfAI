import { expect, test } from './support/fixtures';

test.describe('Settings', () => {
  test('shows the account information and signs out', async ({
    page,
    backend,
    signIn,
    expectNoA11yViolations,
  }) => {
    void backend;
    await signIn();

    await page.getByRole('tab', { name: 'App settings' }).click();
    // The account information Settings shows today is the scan allowance
    // (the screen does not show the signed-in email).
    await expect(page.getByTestId('settings-scan-usage')).toContainText('Scan usage');
    await expect(page.getByTestId('settings-scan-usage')).toContainText('Scans left today:');
    await expect(page.getByRole('button', { name: 'Open the trash' })).toBeVisible();
    await expectNoA11yViolations('Settings');

    // Web has no native alert: sign-out asks with window.confirm.
    page.once('dialog', (dialog) => {
      expect(dialog.type()).toBe('confirm');
      expect(dialog.message()).toContain('Sign out?');
      void dialog.accept();
    });
    await page.getByRole('button', { name: 'Sign out' }).click();

    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole('button', { name: 'Sign in with Google' })).toBeVisible();
    await expect(page.getByRole('tab', { name: 'App settings' })).toHaveCount(0);
    await expectNoA11yViolations('login screen after sign-out');
  });
});
