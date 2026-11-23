// Settings "Export my data" row (#694): confirmation, the request and every
// answer the server can give. The API client is mocked; lib/accountExport is real.
import React from 'react';
import { Alert, Platform } from 'react-native';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

import { AccountExportSetting } from '../../components/AccountExportSetting';
import { BannerContext } from '../../contexts/BannerContext';
import { exportLocale, requestAccountExport } from '../../lib/accountExport';
import { isKeyedWrite } from '../../lib/idempotency';

const mockPost = jest.fn();
jest.mock('../../lib/api', () => ({
  api: { post: (...a: unknown[]) => mockPost(...a) },
}));

const mockShowBanner = jest.fn();

function renderRow() {
  return render(
    <BannerContext.Provider
      value={{ banner: null, showBanner: mockShowBanner, hideBanner: jest.fn() }}
    >
      <AccountExportSetting />
    </BannerContext.Provider>
  );
}

function httpError(status: number, detail: unknown, headers: Record<string, string> = {}) {
  return { message: `status ${status}`, response: { status, data: { detail }, headers } };
}

type Button = { text: string; onPress?: () => void };
const alertButtons = () => (Alert.alert as jest.Mock).mock.calls[0][2] as Button[];

async function confirmAndAnswer(answer: () => Promise<unknown>) {
  mockPost.mockImplementation(answer);
  const utils = await renderRow();
  await fireEvent.press(utils.getByTestId('settings-export-data'));
  const confirm = alertButtons().find((b) => b.text === 'Export')!;
  await act(async () => confirm.onPress!());
  await waitFor(() => expect(mockShowBanner).toHaveBeenCalled());
  return mockShowBanner.mock.calls[0][0];
}

beforeEach(() => {
  jest.clearAllMocks();
  jest.spyOn(Alert, 'alert').mockImplementation(() => {});
});

afterEach(() => {
  jest.restoreAllMocks();
});

describe('Settings Export my data row (#694)', () => {
  it('shows the row as a button with its hint', async () => {
    const utils = await renderRow();
    const row = utils.getByTestId('settings-export-data');
    expect(row.props.accessibilityRole).toBe('button');
    expect(row.props.accessibilityHint).toBe('Get a copy of your data by email');
    expect(utils.getByText('Export my data')).toBeTruthy();
    expect(utils.getByText('Get a copy of your data by email')).toBeTruthy();
  });

  it('asks first, with a translated prompt; Cancel sends nothing', async () => {
    const utils = await renderRow();
    await fireEvent.press(utils.getByTestId('settings-export-data'));
    const [title, body] = (Alert.alert as jest.Mock).mock.calls[0];
    expect(title).toBe('Export your data?');
    expect(body).toMatch(/7 days/);
    expect(alertButtons().map((b) => b.text)).toEqual(['Cancel', 'Export']);
    const cancel = alertButtons().find((b) => b.text === 'Cancel')!;
    expect(cancel.onPress).toBeUndefined();
    expect(mockPost).not.toHaveBeenCalled();
    expect(mockShowBanner).not.toHaveBeenCalled();
  });

  it('on 202: asks the server and says the link comes by email', async () => {
    const banner = await confirmAndAnswer(() => Promise.resolve({ status: 202, data: {} }));
    expect(mockPost).toHaveBeenCalledWith('/account/export', { locale: 'en' });
    expect(banner).toEqual({
      message: "We'll email you a link to your data. It works for 7 days.",
      type: 'info',
      duration: 6000,
    });
  });

  it('on the daily cap: says when the next export is possible', async () => {
    const banner = await confirmAndAnswer(() =>
      Promise.reject(
        httpError(409, { code: 'export_limit', retry_after: 7000 }, { 'retry-after': '7000' })
      )
    );
    expect(banner.message).toBe('You can export once a day. Try again in 2 hours.');
    expect(banner.type).toBe('info');
  });

  it('on the daily cap with an hour or less left: singular', async () => {
    const banner = await confirmAndAnswer(() =>
      Promise.reject(httpError(409, { code: 'export_limit' }, { 'retry-after': '60' }))
    );
    expect(banner.message).toBe('You can export once a day. Try again in 1 hour.');
  });

  it('on the daily cap without Retry-After: reads retry_after from the body', async () => {
    const banner = await confirmAndAnswer(() =>
      Promise.reject(httpError(409, { code: 'export_limit', retry_after: 10_800 }))
    );
    expect(banner.message).toBe('You can export once a day. Try again in 3 hours.');
  });

  it('prefers the Retry-After header over the body', async () => {
    const banner = await confirmAndAnswer(() =>
      Promise.reject(
        httpError(409, { code: 'export_limit', retry_after: 10_800 }, { 'retry-after': '3600' })
      )
    );
    expect(banner.message).toMatch(/in 1 hour\./);
  });

  it.each([['not a number'], [-5]])(
    'ignores an unusable body retry_after (%s)',
    async (retryAfter) => {
      const banner = await confirmAndAnswer(() =>
        Promise.reject(httpError(409, { code: 'export_limit', retry_after: retryAfter }))
      );
      expect(banner.message).toMatch(/24 hours/);
    }
  );

  it('on the daily cap without Retry-After: assumes the whole day', async () => {
    const banner = await confirmAndAnswer(() =>
      Promise.reject(httpError(409, { code: 'export_limit' }))
    );
    expect(banner.message).toMatch(/24 hours/);
  });

  it('on 503 export_not_configured: says export is not available yet', async () => {
    const banner = await confirmAndAnswer(() =>
      Promise.reject(httpError(503, 'export_not_configured'))
    );
    expect(banner).toEqual(
      expect.objectContaining({
        message: "Data export isn't available yet. Try again later.",
        type: 'error',
      })
    );
  });

  it.each([
    ['offline', { message: 'Network Error' }],
    ['a server error', httpError(500, 'boom')],
    ['another 503', httpError(503, 'db_timeout')],
    ['another 409', httpError(409, 'idempotency_in_progress')],
  ])('on %s: says the request failed', async (_name, error) => {
    const banner = await confirmAndAnswer(() => Promise.reject(error));
    expect(banner).toEqual(
      expect.objectContaining({
        message: "Couldn't request your export. Check your connection and try again.",
        type: 'error',
      })
    );
  });

  it('a keyed request already applied counts as queued', async () => {
    const banner = await confirmAndAnswer(() =>
      Promise.reject(httpError(409, 'idempotency_outcome_unknown'))
    );
    expect(banner.message).toMatch(/email you a link/);
  });

  it('ignores presses while a request is running', async () => {
    let finish!: (v: unknown) => void;
    mockPost.mockReturnValue(new Promise((r) => (finish = r)));
    const utils = await renderRow();
    await fireEvent.press(utils.getByTestId('settings-export-data'));
    await act(async () => alertButtons()[1].onPress!());
    const row = utils.getByTestId('settings-export-data');
    expect(row.props.accessibilityState).toEqual({ disabled: true, busy: true });
    await fireEvent.press(row);
    expect(Alert.alert).toHaveBeenCalledTimes(1);
    await act(async () => alertButtons()[1].onPress!());
    expect(mockPost).toHaveBeenCalledTimes(1);
    await act(async () => finish({ status: 202 }));
    expect(utils.getByTestId('settings-export-data').props.accessibilityState).toEqual({
      disabled: false,
      busy: false,
    });
  });

  describe('on web', () => {
    const original = Platform.OS;
    const originalWindow = (globalThis as { window?: unknown }).window;
    beforeEach(() => {
      Object.defineProperty(Platform, 'OS', { value: 'web', configurable: true });
    });
    afterEach(() => {
      Object.defineProperty(Platform, 'OS', { value: original, configurable: true });
      (globalThis as { window?: unknown }).window = originalWindow;
    });

    it.each([
      [true, 1],
      [false, 0],
    ])('window.confirm %s sends %i request(s)', async (answer, posts) => {
      const confirm = jest.fn(() => answer);
      (globalThis as { window?: unknown }).window = { confirm };
      mockPost.mockResolvedValue({ status: 202 });
      const utils = await renderRow();
      await act(async () => fireEvent.press(utils.getByTestId('settings-export-data')));
      expect(confirm).toHaveBeenCalledWith(expect.stringContaining('Export your data?'));
      expect(Alert.alert).not.toHaveBeenCalled();
      expect(mockPost).toHaveBeenCalledTimes(posts);
    });
  });
});

describe('lib/accountExport', () => {
  it('sends the app language when the server writes it, else English', () => {
    expect(exportLocale('de')).toBe('de');
    expect(exportLocale('fr-CA')).toBe('fr-CA');
    expect(exportLocale('pt-BR')).toBe('pt');
    expect(exportLocale('fr')).toBe('en');
    expect(exportLocale('xx')).toBe('en');
    expect(exportLocale(undefined)).toBe('en');
  });

  it('sends the language in the body', async () => {
    mockPost.mockResolvedValue({ status: 202 });
    await expect(requestAccountExport('ja')).resolves.toEqual({ kind: 'queued' });
    expect(mockPost).toHaveBeenCalledWith('/account/export', { locale: 'ja' });
  });

  it('is a keyed write, so the API client adds an Idempotency-Key', () => {
    expect(isKeyedWrite('POST', '/account/export')).toBe(true);
    expect(isKeyedWrite('GET', '/account/export')).toBe(false);
    expect(isKeyedWrite('POST', '/account/export/x')).toBe(false);
  });
});
