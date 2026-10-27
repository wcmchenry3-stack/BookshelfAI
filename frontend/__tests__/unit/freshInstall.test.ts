import { Platform } from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';

import { INSTALL_MARKER_KEY, clearTokensIfFreshInstall } from '../../lib/freshInstall';

jest.mock('@react-native-async-storage/async-storage', () =>
  jest.requireActual('@react-native-async-storage/async-storage/jest/async-storage-mock')
);

jest.mock('../../lib/api', () => ({
  ACCESS_TOKEN_KEY: 'bookshelf_access_token',
  REFRESH_TOKEN_KEY: 'bookshelf_refresh_token',
}));

const mockAddBreadcrumb = jest.fn();
jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: (...args: unknown[]) => mockAddBreadcrumb(...args) },
}));

const mockKeychain = new Map<string, string>();
const mockDeleteItem = jest.fn(async (key: string) => {
  mockKeychain.delete(key);
});
jest.mock('../../lib/storage', () => ({
  getItem: async (key: string) => mockKeychain.get(key) ?? null,
  setItem: async (key: string, value: string) => {
    mockKeychain.set(key, value);
  },
  deleteItem: (key: string) => mockDeleteItem(key),
}));

const originalOS = Platform.OS;

beforeEach(async () => {
  jest.clearAllMocks();
  Object.defineProperty(Platform, 'OS', { value: 'ios', configurable: true });
  mockKeychain.clear();
  mockKeychain.set('bookshelf_access_token', 'old-access');
  mockKeychain.set('bookshelf_refresh_token', 'old-refresh');
  await AsyncStorage.clear();
});

afterAll(() => {
  Object.defineProperty(Platform, 'OS', { value: originalOS, configurable: true });
});

describe('clearTokensIfFreshInstall', () => {
  it('deletes both tokens, leaves a breadcrumb without values and sets the marker', async () => {
    await expect(clearTokensIfFreshInstall()).resolves.toBe(true);

    expect(mockKeychain.size).toBe(0);
    expect(await AsyncStorage.getItem(INSTALL_MARKER_KEY)).not.toBeNull();
    expect(mockAddBreadcrumb).toHaveBeenCalledTimes(1);
    const crumb = mockAddBreadcrumb.mock.calls[0][0];
    expect(crumb).toMatchObject({
      category: 'auth',
      message: 'fresh install: cleared stale tokens',
    });
    expect(JSON.stringify(crumb)).not.toMatch(/old-access|old-refresh/);
  });

  it('only wipes once: the second launch keeps new tokens', async () => {
    await clearTokensIfFreshInstall();
    mockKeychain.set('bookshelf_access_token', 'new-access');

    await expect(clearTokensIfFreshInstall()).resolves.toBe(false);
    expect(mockKeychain.get('bookshelf_access_token')).toBe('new-access');
  });

  it('keeps tokens when the marker is present', async () => {
    await AsyncStorage.setItem(INSTALL_MARKER_KEY, '1');

    await expect(clearTokensIfFreshInstall()).resolves.toBe(false);
    expect(mockKeychain.size).toBe(2);
    expect(mockDeleteItem).not.toHaveBeenCalled();
  });

  it('sets the marker without a breadcrumb when there were no tokens', async () => {
    mockKeychain.clear();

    await expect(clearTokensIfFreshInstall()).resolves.toBe(false);
    expect(mockAddBreadcrumb).not.toHaveBeenCalled();
    expect(await AsyncStorage.getItem(INSTALL_MARKER_KEY)).not.toBeNull();
  });

  it('does not wipe and does not throw when AsyncStorage cannot be read', async () => {
    jest.spyOn(AsyncStorage, 'getItem').mockRejectedValueOnce(new Error('disk'));

    await expect(clearTokensIfFreshInstall()).resolves.toBe(false);
    expect(mockKeychain.size).toBe(2);
    expect(mockDeleteItem).not.toHaveBeenCalled();
  });

  it('leaves the marker unset when SecureStore delete fails, so it retries next launch', async () => {
    mockDeleteItem.mockRejectedValueOnce(new Error('mockKeychain'));

    await expect(clearTokensIfFreshInstall()).resolves.toBe(false);
    expect(await AsyncStorage.getItem(INSTALL_MARKER_KEY)).toBeNull();
  });

  it('does not throw when the marker cannot be written', async () => {
    jest.spyOn(AsyncStorage, 'setItem').mockRejectedValueOnce(new Error('disk full'));

    await expect(clearTokensIfFreshInstall()).resolves.toBe(true);
    expect(mockKeychain.size).toBe(0);
  });

  it('does nothing on web, where tokens live in memory', async () => {
    Object.defineProperty(Platform, 'OS', { value: 'web', configurable: true });

    await expect(clearTokensIfFreshInstall()).resolves.toBe(false);
    expect(mockKeychain.size).toBe(2);
    expect(await AsyncStorage.getItem(INSTALL_MARKER_KEY)).toBeNull();
  });
});
