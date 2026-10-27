import { Platform } from 'react-native';
import AsyncStorage from '@react-native-async-storage/async-storage';

import {
  INSTALL_MARKER_KEY,
  MARKER_DONE,
  MARKER_PENDING,
  clearTokensIfFreshInstall,
  markWipePending,
  resetInstallCheckForTests,
} from '../../lib/freshInstall';

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
const mockStorageGetItem = jest.fn(async (key: string) => mockKeychain.get(key) ?? null);
jest.mock('../../lib/storage', () => ({
  getItem: (key: string) => mockStorageGetItem(key),
  setItem: async (key: string, value: string) => {
    mockKeychain.set(key, value);
  },
  deleteItem: (key: string) => mockDeleteItem(key),
}));

const originalOS = Platform.OS;

beforeEach(async () => {
  jest.clearAllMocks();
  resetInstallCheckForTests();
  Object.defineProperty(Platform, 'OS', { value: 'ios', configurable: true });
  mockKeychain.clear();
  mockKeychain.set('bookshelf_access_token', 'old-access');
  mockKeychain.set('bookshelf_refresh_token', 'old-refresh');
  await AsyncStorage.clear();
});

afterAll(() => {
  Object.defineProperty(Platform, 'OS', { value: originalOS, configurable: true });
});

// Simulates the next app launch: the memoised check is forgotten.
function nextLaunch() {
  resetInstallCheckForTests();
}

describe('clearTokensIfFreshInstall', () => {
  it('deletes both tokens, leaves a breadcrumb without values and sets the marker', async () => {
    await expect(clearTokensIfFreshInstall()).resolves.toBe('cleared');

    expect(mockKeychain.size).toBe(0);
    expect(await AsyncStorage.getItem(INSTALL_MARKER_KEY)).toBe(MARKER_DONE);
    expect(mockAddBreadcrumb).toHaveBeenCalledTimes(1);
    const crumb = mockAddBreadcrumb.mock.calls[0][0];
    expect(crumb).toMatchObject({
      category: 'auth',
      message: 'fresh install: cleared stale tokens',
    });
    expect(JSON.stringify(crumb)).not.toMatch(/old-access|old-refresh/);
  });

  it('marks the wipe pending before deleting the tokens', async () => {
    let markerDuringDelete: string | null | undefined;
    mockDeleteItem.mockImplementationOnce(async (key: string) => {
      markerDuringDelete = await AsyncStorage.getItem(INSTALL_MARKER_KEY);
      mockKeychain.delete(key);
    });

    await clearTokensIfFreshInstall();

    expect(markerDuringDelete).toBe(MARKER_PENDING);
  });

  it('only wipes once: the second launch keeps new tokens', async () => {
    await clearTokensIfFreshInstall();
    mockKeychain.set('bookshelf_access_token', 'new-access');
    nextLaunch();

    await expect(clearTokensIfFreshInstall()).resolves.toBe('no_change');
    expect(mockKeychain.get('bookshelf_access_token')).toBe('new-access');
  });

  it('keeps tokens when the marker is present', async () => {
    await AsyncStorage.setItem(INSTALL_MARKER_KEY, MARKER_DONE);

    await expect(clearTokensIfFreshInstall()).resolves.toBe('no_change');
    expect(mockKeychain.size).toBe(2);
    expect(mockDeleteItem).not.toHaveBeenCalled();
  });

  it('sets the marker without a breadcrumb when there were no tokens', async () => {
    mockKeychain.clear();

    await expect(clearTokensIfFreshInstall()).resolves.toBe('no_change');
    expect(mockAddBreadcrumb).not.toHaveBeenCalled();
    expect(await AsyncStorage.getItem(INSTALL_MARKER_KEY)).toBe(MARKER_DONE);
  });

  it('reports check_failed, deletes nothing and does not throw when AsyncStorage keys cannot be read', async () => {
    // A fresh install cannot be told from an existing one; AuthProvider ends
    // the launch signed out and gates the request layer.
    (AsyncStorage.getAllKeys as jest.Mock).mockRejectedValueOnce(new Error('disk'));

    await expect(clearTokensIfFreshInstall()).resolves.toBe('check_failed');
    expect(mockKeychain.size).toBe(2);
    expect(mockDeleteItem).not.toHaveBeenCalled();
  });

  it('reports check_failed when the marker cannot be read', async () => {
    await AsyncStorage.setItem(INSTALL_MARKER_KEY, MARKER_DONE);
    (AsyncStorage.getItem as jest.Mock).mockRejectedValueOnce(new Error('disk'));

    await expect(clearTokensIfFreshInstall()).resolves.toBe('check_failed');
    expect(mockKeychain.size).toBe(2);
  });

  it('after a failed check, the next launch wipes first even once the scan queue is written', async () => {
    // Fresh install, but AsyncStorage could not be read.
    (AsyncStorage.getAllKeys as jest.Mock).mockRejectedValueOnce(new Error('disk'));
    await expect(clearTokensIfFreshInstall()).resolves.toBe('check_failed');
    expect(mockKeychain.size).toBe(2);
    expect(await AsyncStorage.getItem(INSTALL_MARKER_KEY)).toBe(MARKER_PENDING);

    // ScanJobProvider persists its queue during that launch.
    await AsyncStorage.setItem('scan_job_queue', '[]');
    nextLaunch();

    await expect(clearTokensIfFreshInstall()).resolves.toBe('cleared');
    expect(mockKeychain.size).toBe(0);
    expect(await AsyncStorage.getItem(INSTALL_MARKER_KEY)).toBe(MARKER_DONE);
  });

  it('still reports check_failed when the pending marker cannot be written either', async () => {
    (AsyncStorage.getAllKeys as jest.Mock).mockRejectedValueOnce(new Error('disk'));
    (AsyncStorage.setItem as jest.Mock).mockRejectedValueOnce(new Error('disk'));

    await expect(clearTokensIfFreshInstall()).resolves.toBe('check_failed');
    expect(mockKeychain.size).toBe(2);
  });

  it('markWipePending makes the next launch wipe a known install', async () => {
    await AsyncStorage.setItem(INSTALL_MARKER_KEY, MARKER_DONE);

    await markWipePending();

    await expect(clearTokensIfFreshInstall()).resolves.toBe('cleared');
    expect(mockKeychain.size).toBe(0);
  });

  it('markWipePending never rejects', async () => {
    (AsyncStorage.setItem as jest.Mock).mockRejectedValueOnce(new Error('disk'));

    await expect(markWipePending()).resolves.toBeUndefined();
  });

  it('wipes when the marker holds an unknown value (fail closed)', async () => {
    await AsyncStorage.setItem('scan_job_queue', '[]');
    await AsyncStorage.setItem(INSTALL_MARKER_KEY, 'corrupt');

    await expect(clearTokensIfFreshInstall()).resolves.toBe('cleared');
    expect(mockKeychain.size).toBe(0);
    expect(await AsyncStorage.getItem(INSTALL_MARKER_KEY)).toBe(MARKER_DONE);
  });

  it('keeps tokens and sets the marker when upgrading an existing install', async () => {
    // An install from before #722: it has app data but no marker.
    await AsyncStorage.setItem('scan_job_queue', '[]');

    await expect(clearTokensIfFreshInstall()).resolves.toBe('no_change');
    expect(mockKeychain.get('bookshelf_access_token')).toBe('old-access');
    expect(mockKeychain.get('bookshelf_refresh_token')).toBe('old-refresh');
    expect(mockDeleteItem).not.toHaveBeenCalled();
    expect(await AsyncStorage.getItem(INSTALL_MARKER_KEY)).toBe(MARKER_DONE);
    expect(mockAddBreadcrumb).not.toHaveBeenCalledWith(
      expect.objectContaining({ message: 'fresh install: cleared stale tokens' })
    );
  });

  it('checks once per launch: repeat calls share the first result', async () => {
    const first = clearTokensIfFreshInstall();
    const second = clearTokensIfFreshInstall();

    expect(second).toBe(first);
    await expect(second).resolves.toBe('cleared');
    expect(mockDeleteItem).toHaveBeenCalledTimes(2); // one call per token
  });

  it('retries a failed wipe on the next launch even after the scan queue was written', async () => {
    mockDeleteItem.mockRejectedValueOnce(new Error('keychain locked'));

    await expect(clearTokensIfFreshInstall()).resolves.toBe('wipe_failed');
    expect(mockKeychain.size).toBe(2);
    expect(await AsyncStorage.getItem(INSTALL_MARKER_KEY)).toBe(MARKER_PENDING);

    // During that launch ScanJobProvider persists its (empty) queue, so the
    // next launch no longer sees an empty AsyncStorage.
    await AsyncStorage.setItem('scan_job_queue', '[]');
    nextLaunch();

    await expect(clearTokensIfFreshInstall()).resolves.toBe('cleared');
    expect(mockKeychain.size).toBe(0);
    expect(await AsyncStorage.getItem(INSTALL_MARKER_KEY)).toBe(MARKER_DONE);
  });

  it('reports wipe_failed when reading the tokens throws', async () => {
    mockStorageGetItem.mockRejectedValueOnce(new Error('keychain locked'));

    await expect(clearTokensIfFreshInstall()).resolves.toBe('wipe_failed');
    expect(await AsyncStorage.getItem(INSTALL_MARKER_KEY)).toBe(MARKER_PENDING);
  });

  it('wipes again next launch when the done marker cannot be written', async () => {
    const setItem = AsyncStorage.setItem as jest.Mock;
    const realSetItem = setItem.getMockImplementation()!;
    setItem
      .mockImplementationOnce(realSetItem) // 'pending' is written
      .mockRejectedValueOnce(new Error('disk full')); // '1' is not

    await expect(clearTokensIfFreshInstall()).resolves.toBe('cleared');
    expect(mockKeychain.size).toBe(0);
    expect(await AsyncStorage.getItem(INSTALL_MARKER_KEY)).toBe(MARKER_PENDING);

    mockKeychain.set('bookshelf_access_token', 'new-access');
    nextLaunch();
    await expect(clearTokensIfFreshInstall()).resolves.toBe('cleared');
    expect(mockKeychain.size).toBe(0);
    expect(await AsyncStorage.getItem(INSTALL_MARKER_KEY)).toBe(MARKER_DONE);
  });

  it('still wipes when no marker can be written', async () => {
    (AsyncStorage.setItem as jest.Mock)
      .mockRejectedValueOnce(new Error('disk full'))
      .mockRejectedValueOnce(new Error('disk full'));

    await expect(clearTokensIfFreshInstall()).resolves.toBe('cleared');
    expect(mockKeychain.size).toBe(0);
  });

  it('does nothing on web, where tokens live in memory', async () => {
    Object.defineProperty(Platform, 'OS', { value: 'web', configurable: true });

    await expect(clearTokensIfFreshInstall()).resolves.toBe('no_change');
    expect(mockKeychain.size).toBe(2);
    expect(await AsyncStorage.getItem(INSTALL_MARKER_KEY)).toBeNull();
  });
});
