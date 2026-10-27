import React from 'react';
import { Alert, Platform } from 'react-native';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

import SettingsScreen from '../../app/(tabs)/settings';
import { AuthProvider } from '../../contexts/AuthContext';

const mockPost = jest.fn();
const mockReplace = jest.fn();
jest.mock('expo-router', () => ({
  useRouter: () => ({ replace: (...a: unknown[]) => mockReplace(...a) }),
  useSegments: () => ['(tabs)'],
}));
jest.mock('../../lib/api', () => ({
  ACCESS_TOKEN_KEY: 'access',
  REFRESH_TOKEN_KEY: 'refresh',
  api: { post: (...a: unknown[]) => mockPost(...a), get: jest.fn() },
  setAuthFailureCallback: jest.fn(),
}));
const mockGetItem = jest.fn();
const mockDeleteItem = jest.fn();
jest.mock('../../lib/storage', () => ({
  getItem: (...a: unknown[]) => mockGetItem(...a),
  setItem: jest.fn(),
  deleteItem: (...a: unknown[]) => mockDeleteItem(...a),
}));
jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: { colors: { background: '#fff', text: '#000', error: '#f00' }, spacing: { md: 16 } },
  }),
}));
jest.mock('../../hooks/useScanJobs', () => ({ useScanJobs: () => ({ usageRefreshKey: 0 }) }));
jest.mock('../../hooks/useScanUsage', () => ({ useScanUsage: () => null }));
jest.mock('../../components/ThemeToggleButton', () => ({ ThemeToggleButton: () => null }));

async function setup() {
  // The real AuthProvider, so logout() really deletes the tokens and the
  // redirect effect really runs.
  const utils = await render(
    <AuthProvider>
      <SettingsScreen />
    </AuthProvider>
  );
  await waitFor(() => expect(mockGetItem).toHaveBeenCalledWith('access'));
  fireEvent.press(utils.getByTestId('settings-sign-out'));
  return utils;
}

function confirmButtons() {
  return (Alert.alert as jest.Mock).mock.calls[0][2] as {
    text: string;
    onPress?: () => void;
  }[];
}
const pressConfirm = () => confirmButtons().find((b) => b.text === 'Sign out')!.onPress!();

describe('Settings sign out', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetItem.mockImplementation(async (k: string) => (k === 'access' ? 'at' : 'refresh-abc'));
    mockDeleteItem.mockResolvedValue(undefined);
    mockPost.mockResolvedValue({});
    jest.spyOn(Alert, 'alert').mockImplementation(() => {});
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  async function expectSignedOut() {
    await waitFor(() => expect(mockReplace).toHaveBeenCalledWith('/(auth)/login'));
    expect(mockDeleteItem).toHaveBeenCalledWith('access');
    expect(mockDeleteItem).toHaveBeenCalledWith('refresh');
  }

  it('confirm: revokes the refresh token, deletes tokens, redirects to login', async () => {
    await setup();
    expect(mockDeleteItem).not.toHaveBeenCalled();
    pressConfirm();
    await expectSignedOut();
    expect(mockPost).toHaveBeenCalledWith(
      '/auth/logout',
      { refresh_token: 'refresh-abc' },
      { timeout: 5000 }
    );
  });

  it('still signs out locally when the server call fails', async () => {
    mockPost.mockRejectedValue(new Error('network'));
    await setup();
    pressConfirm();
    await expectSignedOut();
  });

  it('cancel does nothing', async () => {
    await setup();
    confirmButtons()
      .find((b) => b.text === 'Cancel')!
      .onPress?.();
    expect(mockPost).not.toHaveBeenCalled();
    expect(mockDeleteItem).not.toHaveBeenCalled();
    expect(mockReplace).not.toHaveBeenCalled();
  });

  it('disables the row while signing out and ignores a second confirm', async () => {
    let finish: () => void = () => {};
    mockPost.mockReturnValue(new Promise<void>((r) => (finish = r)));
    const { getByTestId } = await setup();
    pressConfirm();
    await waitFor(() =>
      expect(getByTestId('settings-sign-out').props.accessibilityState).toMatchObject({
        disabled: true,
        busy: true,
      })
    );
    fireEvent.press(getByTestId('settings-sign-out'));
    pressConfirm();
    expect(Alert.alert).toHaveBeenCalledTimes(1);
    expect(mockPost).toHaveBeenCalledTimes(1);
    await act(async () => finish());
    await expectSignedOut();
  });

  describe('web', () => {
    const originalOS = Platform.OS;
    const originalWindow = (global as { window?: unknown }).window;
    let confirm: jest.Mock;

    beforeEach(() => {
      Platform.OS = 'web';
      confirm = jest.fn();
      (global as unknown as { window: unknown }).window = { confirm };
    });
    afterEach(() => {
      Platform.OS = originalOS;
      (global as unknown as { window: unknown }).window = originalWindow;
    });

    it('confirming window.confirm signs out', async () => {
      confirm.mockReturnValue(true);
      await setup();
      expect(Alert.alert).not.toHaveBeenCalled();
      await expectSignedOut();
      expect(mockPost).toHaveBeenCalledTimes(1);
    });

    it('cancelling window.confirm does nothing', async () => {
      confirm.mockReturnValue(false);
      await setup();
      expect(confirm).toHaveBeenCalledTimes(1);
      expect(mockPost).not.toHaveBeenCalled();
      expect(mockDeleteItem).not.toHaveBeenCalled();
      expect(mockReplace).not.toHaveBeenCalled();
    });
  });

  // Keep last: fake timers leave the React test renderer unusable for later tests.
  it('a stalled server call cannot block the local sign-out', async () => {
    mockPost.mockReturnValue(new Promise(() => {}));
    await setup();
    jest.useFakeTimers();
    pressConfirm();
    await act(async () => {
      await jest.advanceTimersByTimeAsync(4000);
    });
    expect(mockDeleteItem).not.toHaveBeenCalled();
    await act(async () => {
      await jest.advanceTimersByTimeAsync(1500);
    });
    jest.useRealTimers();
    await expectSignedOut();
  });
});
