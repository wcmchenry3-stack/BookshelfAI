import React from 'react';
import { Alert, Platform } from 'react-native';
import { fireEvent, render, waitFor } from '@testing-library/react-native';

import SettingsScreen from '../../app/(tabs)/settings';
import { AuthContext } from '../../contexts/AuthContext';

const mockPost = jest.fn();
const mockGetItem = jest.fn();
jest.mock('../../lib/api', () => ({
  REFRESH_TOKEN_KEY: 'bookshelf_refresh_token',
  api: { post: (...a: unknown[]) => mockPost(...a), get: jest.fn() },
}));
jest.mock('../../lib/storage', () => ({ getItem: (...a: unknown[]) => mockGetItem(...a) }));
jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: { colors: { background: '#fff', text: '#000', error: '#f00' }, spacing: { md: 16 } },
  }),
}));
jest.mock('../../hooks/useScanJobs', () => ({ useScanJobs: () => ({ usageRefreshKey: 0 }) }));
jest.mock('../../hooks/useScanUsage', () => ({ useScanUsage: () => null }));
jest.mock('../../components/ThemeToggleButton', () => ({ ThemeToggleButton: () => null }));

const logout = jest.fn();

async function setup() {
  const utils = await render(
    <AuthContext.Provider
      value={{
        isAuthenticated: true,
        isLoading: false,
        login: jest.fn(),
        testLogin: jest.fn(),
        logout,
      }}
    >
      <SettingsScreen />
    </AuthContext.Provider>
  );
  fireEvent.press(utils.getByTestId('settings-sign-out'));
  return utils;
}

function confirmButtons() {
  return (Alert.alert as jest.Mock).mock.calls[0][2] as {
    text: string;
    onPress?: () => void;
  }[];
}

describe('Settings sign out', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockGetItem.mockResolvedValue('refresh-abc');
    mockPost.mockResolvedValue({});
    jest.spyOn(Alert, 'alert').mockImplementation(() => {});
  });

  it('confirm: revokes the refresh token server-side then logs out', async () => {
    await setup();
    expect(logout).not.toHaveBeenCalled();
    confirmButtons().find((b) => b.text === 'Sign out')!.onPress!();
    await waitFor(() => expect(logout).toHaveBeenCalledTimes(1));
    expect(mockPost).toHaveBeenCalledWith('/auth/logout', { refresh_token: 'refresh-abc' });
  });

  it('still signs out locally when the server call fails', async () => {
    mockPost.mockRejectedValue(new Error('network'));
    await setup();
    confirmButtons().find((b) => b.text === 'Sign out')!.onPress!();
    await waitFor(() => expect(logout).toHaveBeenCalledTimes(1));
  });

  it('cancel does nothing', async () => {
    await setup();
    const cancel = confirmButtons().find((b) => b.text === 'Cancel')!;
    cancel.onPress?.();
    expect(mockPost).not.toHaveBeenCalled();
    expect(logout).not.toHaveBeenCalled();
  });

  it('uses window.confirm on web', async () => {
    const original = Platform.OS;
    Platform.OS = 'web';
    const confirm = jest.fn().mockReturnValue(true);
    (global as unknown as { window: unknown }).window = { confirm };
    try {
      await setup();
      expect(Alert.alert).not.toHaveBeenCalled();
      await waitFor(() => expect(logout).toHaveBeenCalledTimes(1));
      expect(mockPost).toHaveBeenCalledWith('/auth/logout', { refresh_token: 'refresh-abc' });
    } finally {
      Platform.OS = original;
    }
  });
});
