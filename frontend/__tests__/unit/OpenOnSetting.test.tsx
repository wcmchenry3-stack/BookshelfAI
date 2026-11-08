import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

import { OpenOnSetting } from '../../components/OpenOnSetting';
import { AuthContext } from '../../contexts/AuthContext';
import { BannerContext } from '../../contexts/BannerContext';
import type { DefaultTab } from '../../lib/defaultTab';

jest.mock('expo-router', () => ({
  useRouter: () => ({ replace: jest.fn() }),
  useSegments: () => ['(tabs)'],
}));
const mockGet = jest.fn();
const mockPatch = jest.fn();
jest.mock('../../lib/api', () => ({
  api: {
    get: (...a: unknown[]) => mockGet(...a),
    patch: (...a: unknown[]) => mockPatch(...a),
  },
}));
const mockBreadcrumb = jest.fn();
jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: (...a: unknown[]) => mockBreadcrumb(...a) },
}));
jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: {
        text: '#000',
        textSecondary: '#555',
        scrim: '#0008',
        surface: '#fff',
        outline: '#ccc',
        onSurface: '#000',
        primary: '#00f',
      },
      typography: { fontSizeBase: 16 },
    },
  }),
}));

const mockShowBanner = jest.fn();

function renderRow(defaultTab: DefaultTab = 'scan') {
  return render(
    <AuthContext.Provider
      value={{
        isAuthenticated: true,
        isLoading: false,
        defaultTab,
        login: async () => {},
        testLogin: async () => {},
        logout: async () => {},
      }}
    >
      <BannerContext.Provider
        value={{ banner: null, showBanner: mockShowBanner, hideBanner: jest.fn() }}
      >
        <OpenOnSetting />
      </BannerContext.Provider>
    </AuthContext.Provider>
  );
}

const value = (utils: { getByTestId: (id: string) => { props: Record<string, unknown> } }) =>
  utils.getByTestId('settings-open-on-value').props.children;

beforeEach(() => {
  jest.clearAllMocks();
  // The mount re-read fails unless a test says otherwise, so the row keeps
  // the value AuthContext gave it.
  mockGet.mockRejectedValue({ message: 'Network Error' });
});

describe('Settings "Open on" row (#913)', () => {
  it.each([
    ['scan', 'Scan'],
    ['wishlist', 'Wishlist'],
    ['my-books', 'My Books'],
  ] as const)('shows the current choice %s', async (tab, label) => {
    const utils = await renderRow(tab);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(value(utils)).toBe(label);
    const row = utils.getByTestId('settings-open-on');
    expect(row.props.accessibilityLabel).toBe(`Open on: ${label}`);
    expect(row.props.accessibilityHint).toBe('Choose the screen the app opens on');
    expect(utils.getByText('Open on')).toBeTruthy();
  });

  it('re-reads the value from the server on mount', async () => {
    mockGet.mockResolvedValue({ data: { default_tab: 'wishlist' } });
    const utils = await renderRow('scan');
    await waitFor(() => expect(value(utils)).toBe('Wishlist'));
    expect(mockGet).toHaveBeenCalledWith('/auth/me', expect.anything());
  });

  it('opens a picker with the three choices, the current one checked', async () => {
    const utils = await renderRow('my-books');
    await fireEvent.press(utils.getByTestId('settings-open-on'));
    expect(utils.getByText('Open the app on')).toBeTruthy();
    for (const tab of ['scan', 'wishlist', 'my-books']) {
      const option = utils.getByTestId(`open-on-option-${tab}`);
      expect(option.props.accessibilityRole).toBe('radio');
      expect(option.props.accessibilityState).toEqual({ checked: tab === 'my-books' });
    }
  });

  it('picking a choice saves it and shows it at once (optimistic)', async () => {
    let answer!: (v: unknown) => void;
    mockPatch.mockReturnValue(new Promise((r) => (answer = r)));
    const utils = await renderRow('scan');
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    await fireEvent.press(utils.getByTestId('settings-open-on'));
    await fireEvent.press(utils.getByTestId('open-on-option-wishlist'));

    expect(value(utils)).toBe('Wishlist');
    expect(mockPatch).toHaveBeenCalledWith('/users/me/preferences', { default_tab: 'wishlist' });
    await act(async () => answer({ data: { default_tab: 'wishlist' } }));
    expect(value(utils)).toBe('Wishlist');
    expect(mockShowBanner).not.toHaveBeenCalled();
  });

  it('a failed save restores the old value and shows a translated error banner', async () => {
    mockPatch.mockRejectedValue({ response: { status: 503, data: { detail: 'db_timeout' } } });
    const utils = await renderRow('wishlist');
    await waitFor(() => expect(mockGet).toHaveBeenCalled());

    await fireEvent.press(utils.getByTestId('settings-open-on'));
    await act(async () => {
      await fireEvent.press(utils.getByTestId('open-on-option-my-books'));
    });

    await waitFor(() => expect(value(utils)).toBe('Wishlist'));
    expect(mockShowBanner).toHaveBeenCalledWith({
      message: "Couldn't save your choice. Try again.",
      type: 'error',
    });
    expect(mockBreadcrumb).toHaveBeenCalledWith(
      expect.objectContaining({ data: { status: 503, code: 'db_timeout' } })
    );
  });

  it('picking the current choice saves nothing', async () => {
    const utils = await renderRow('scan');
    await fireEvent.press(utils.getByTestId('settings-open-on'));
    await fireEvent.press(utils.getByTestId('open-on-option-scan'));
    expect(mockPatch).not.toHaveBeenCalled();
  });

  it("a late mount re-read does not overwrite the user's choice", async () => {
    let answerGet!: (v: unknown) => void;
    mockGet.mockReturnValue(new Promise((r) => (answerGet = r)));
    mockPatch.mockResolvedValue({ data: { default_tab: 'my-books' } });
    const utils = await renderRow('scan');

    await fireEvent.press(utils.getByTestId('settings-open-on'));
    await act(async () => {
      await fireEvent.press(utils.getByTestId('open-on-option-my-books'));
    });
    await act(async () => answerGet({ data: { default_tab: 'wishlist' } }));

    expect(value(utils)).toBe('My Books');
  });

  it('cancel and the backdrop close the picker without saving', async () => {
    const utils = await renderRow('scan');
    await fireEvent.press(utils.getByTestId('settings-open-on'));
    await fireEvent.press(utils.getByTestId('open-on-cancel'));
    await fireEvent.press(utils.getByTestId('settings-open-on'));
    await fireEvent.press(utils.getByTestId('open-on-backdrop'));
    expect(mockPatch).not.toHaveBeenCalled();
    expect(value(utils)).toBe('Scan');
  });
});
