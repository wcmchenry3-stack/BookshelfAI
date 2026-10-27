import React, { useContext } from 'react';
import { Text } from 'react-native';
import { act, render, waitFor } from '@testing-library/react-native';

import { AuthContext, AuthProvider, SESSION_CHECK_TIMEOUT_MS } from '../../contexts/AuthContext';

import AsyncStorage from '@react-native-async-storage/async-storage';

import { INSTALL_MARKER_KEY } from '../../lib/freshInstall';

const mockPost = jest.fn();
const mockGet = jest.fn();
const mockSetAuthFailureCallback = jest.fn();
jest.mock('../../lib/api', () => ({
  api: {
    post: (...args: unknown[]) => mockPost(...args),
    get: (...args: unknown[]) => mockGet(...args),
  },
  ACCESS_TOKEN_KEY: 'bookshelf_access_token',
  REFRESH_TOKEN_KEY: 'bookshelf_refresh_token',
  setAuthFailureCallback: (...args: unknown[]) => mockSetAuthFailureCallback(...args),
}));

jest.mock('@react-native-async-storage/async-storage', () =>
  jest.requireActual('@react-native-async-storage/async-storage/jest/async-storage-mock')
);

const mockAddBreadcrumb = jest.fn();
jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: (...args: unknown[]) => mockAddBreadcrumb(...args) },
}));

const mockGetItem = jest.fn();
const mockSetItem = jest.fn();
const mockDeleteItem = jest.fn();
jest.mock('expo-secure-store', () => ({
  getItemAsync: (...args: unknown[]) => mockGetItem(...args),
  setItemAsync: (...args: unknown[]) => mockSetItem(...args),
  deleteItemAsync: (...args: unknown[]) => mockDeleteItem(...args),
}));

const mockReplace = jest.fn();
jest.mock('expo-router', () => ({
  useRouter: () => ({ replace: mockReplace }),
  useSegments: () => [],
}));

function AuthDisplay() {
  const { isAuthenticated, isLoading, login, logout } = useContext(AuthContext);
  return <Text testID="state">{isLoading ? 'loading' : isAuthenticated ? 'authed' : 'guest'}</Text>;
}

beforeEach(async () => {
  jest.clearAllMocks();
  mockGetItem.mockResolvedValue(null);
  mockSetItem.mockResolvedValue(undefined);
  mockDeleteItem.mockResolvedValue(undefined);
  mockGet.mockResolvedValue({ data: {} });
  // Not a fresh install unless a test says so.
  await AsyncStorage.clear();
  await AsyncStorage.setItem(INSTALL_MARKER_KEY, '1');
});

describe('AuthProvider', () => {
  it('starts in loading state', async () => {
    mockGetItem.mockReturnValue(new Promise(() => {}));
    const { getByTestId } = await render(
      <AuthProvider>
        <AuthDisplay />
      </AuthProvider>
    );
    expect(getByTestId('state').props.children).toBe('loading');
  });

  it('resolves to guest when no stored token', async () => {
    mockGetItem.mockResolvedValue(null);
    const { getByTestId } = await render(
      <AuthProvider>
        <AuthDisplay />
      </AuthProvider>
    );
    await waitFor(() => expect(getByTestId('state').props.children).toBe('guest'));
  });

  it('resolves to authed when token found in SecureStore', async () => {
    mockGetItem.mockResolvedValue('some-token');
    const { getByTestId } = await render(
      <AuthProvider>
        <AuthDisplay />
      </AuthProvider>
    );
    await waitFor(() => expect(getByTestId('state').props.children).toBe('authed'));
  });

  it('login stores tokens and sets authenticated', async () => {
    mockPost.mockResolvedValue({
      data: { access_token: 'acc', refresh_token: 'ref' },
    });
    mockGetItem.mockResolvedValue(null);

    let capturedLogin: (token: string) => Promise<void> = async () => {};
    function LoginCapture() {
      const { login } = useContext(AuthContext);
      // eslint-disable-next-line react-hooks/globals
      capturedLogin = login;
      return null;
    }

    const { getByTestId } = await render(
      <AuthProvider>
        <AuthDisplay />
        <LoginCapture />
      </AuthProvider>
    );
    await waitFor(() => expect(getByTestId('state').props.children).toBe('guest'));

    await act(async () => {
      await capturedLogin('google-id-token');
    });

    expect(mockPost).toHaveBeenCalledWith('/auth/google', { id_token: 'google-id-token' });
    expect(mockSetItem).toHaveBeenCalledWith('bookshelf_access_token', 'acc');
    expect(mockSetItem).toHaveBeenCalledWith('bookshelf_refresh_token', 'ref');
    expect(getByTestId('state').props.children).toBe('authed');
  });

  it('logout clears tokens and sets unauthenticated', async () => {
    mockGetItem.mockResolvedValue('some-token');

    let capturedLogout: () => Promise<void> = async () => {};
    function LogoutCapture() {
      const { logout } = useContext(AuthContext);
      // eslint-disable-next-line react-hooks/globals
      capturedLogout = logout;
      return null;
    }

    const { getByTestId } = await render(
      <AuthProvider>
        <AuthDisplay />
        <LogoutCapture />
      </AuthProvider>
    );
    await waitFor(() => expect(getByTestId('state').props.children).toBe('authed'));

    await act(async () => {
      await capturedLogout();
    });

    expect(mockDeleteItem).toHaveBeenCalledWith('bookshelf_access_token');
    expect(mockDeleteItem).toHaveBeenCalledWith('bookshelf_refresh_token');
    expect(getByTestId('state').props.children).toBe('guest');
  });
});

// Keychain-backed SecureStore mock keyed by name, so a delete really removes
// the token for later reads.
function useKeychain(initial: Record<string, string>) {
  const keychain = new Map(Object.entries(initial));
  mockGetItem.mockImplementation(async (key: string) => keychain.get(key) ?? null);
  mockSetItem.mockImplementation(async (key: string, value: string) => {
    keychain.set(key, value);
  });
  mockDeleteItem.mockImplementation(async (key: string) => {
    keychain.delete(key);
  });
  return keychain;
}

const BOTH_TOKENS = {
  bookshelf_access_token: 'old-access',
  bookshelf_refresh_token: 'old-refresh',
};

function renderProvider() {
  return render(
    <AuthProvider>
      <AuthDisplay />
    </AuthProvider>
  );
}

describe('AuthProvider fresh install (#722)', () => {
  it('clears tokens left by a previous install and shows the login screen', async () => {
    await AsyncStorage.clear();
    const keychain = useKeychain(BOTH_TOKENS);

    const { getByTestId } = await renderProvider();

    await waitFor(() => expect(getByTestId('state').props.children).toBe('guest'));
    expect(keychain.size).toBe(0);
    expect(mockGet).not.toHaveBeenCalled();
    expect(mockReplace).toHaveBeenCalledWith('/(auth)/login');
    expect(await AsyncStorage.getItem(INSTALL_MARKER_KEY)).not.toBeNull();
    expect(mockAddBreadcrumb).toHaveBeenCalledWith(
      expect.objectContaining({ category: 'auth', message: 'fresh install: cleared stale tokens' })
    );
  });

  it('keeps tokens when the install marker is present', async () => {
    const keychain = useKeychain(BOTH_TOKENS);

    const { getByTestId } = await renderProvider();

    await waitFor(() => expect(getByTestId('state').props.children).toBe('authed'));
    expect(keychain.get('bookshelf_access_token')).toBe('old-access');
    expect(keychain.get('bookshelf_refresh_token')).toBe('old-refresh');
    expect(mockReplace).not.toHaveBeenCalledWith('/(auth)/login');
  });

  it('does not wipe tokens when AsyncStorage cannot be read', async () => {
    await AsyncStorage.clear();
    const spy = jest.spyOn(AsyncStorage, 'getItem').mockRejectedValueOnce(new Error('disk'));
    const keychain = useKeychain(BOTH_TOKENS);

    const { getByTestId } = await renderProvider();

    await waitFor(() => expect(getByTestId('state').props.children).toBe('authed'));
    expect(keychain.size).toBe(2);
    spy.mockRestore();
  });
});

describe('AuthProvider launch validation (#722)', () => {
  it('confirms the stored session with GET /auth/me while loading', async () => {
    useKeychain(BOTH_TOKENS);
    let resolveMe: (v: unknown) => void = () => {};
    mockGet.mockReturnValue(new Promise((r) => (resolveMe = r)));

    const { getByTestId } = await renderProvider();

    await waitFor(() => expect(mockGet).toHaveBeenCalledWith('/auth/me', expect.anything()));
    expect(getByTestId('state').props.children).toBe('loading');
    await act(async () => resolveMe({ data: {} }));
    await waitFor(() => expect(getByTestId('state').props.children).toBe('authed'));
  });

  it('signs out when /auth/me is 401 and the refresh was rejected', async () => {
    const keychain = useKeychain(BOTH_TOKENS);
    // The real interceptor deletes both tokens and calls onAuthFailure when
    // /auth/refresh returns 401; simulate that, then reject with the 401.
    mockGet.mockImplementation(async () => {
      keychain.clear();
      const onAuthFailure = mockSetAuthFailureCallback.mock.calls.at(-1)?.[0];
      await onAuthFailure?.();
      throw { response: { status: 401 } };
    });

    const { getByTestId } = await renderProvider();

    await waitFor(() => expect(getByTestId('state').props.children).toBe('guest'));
    expect(mockReplace).toHaveBeenCalledWith('/(auth)/login');
  });

  it('signs out when /auth/me is 401 and there is no refresh token', async () => {
    const keychain = useKeychain({ bookshelf_access_token: 'old-access' });
    mockGet.mockRejectedValue({ response: { status: 401 } });

    const { getByTestId } = await renderProvider();

    await waitFor(() => expect(getByTestId('state').props.children).toBe('guest'));
    expect(keychain.size).toBe(0);
  });

  it('stays signed in on a 401 when the tokens survive (refresh failed transiently)', async () => {
    useKeychain(BOTH_TOKENS);
    mockGet.mockRejectedValue({ response: { status: 401 } });

    const { getByTestId } = await renderProvider();

    await waitFor(() => expect(getByTestId('state').props.children).toBe('authed'));
  });

  it.each([
    ['network error', { message: 'Network Error' }],
    ['5xx', { response: { status: 503 } }],
    ['403', { response: { status: 403 } }],
  ])('stays signed in on a %s', async (_label, error) => {
    const keychain = useKeychain(BOTH_TOKENS);
    mockGet.mockRejectedValue(error);

    const { getByTestId } = await renderProvider();

    await waitFor(() => expect(getByTestId('state').props.children).toBe('authed'));
    expect(keychain.size).toBe(2);
  });

  it('stops waiting on a hung /auth/me and stays signed in', async () => {
    jest.useFakeTimers();
    try {
      useKeychain(BOTH_TOKENS);
      mockGet.mockReturnValue(new Promise(() => {}));

      const { getByTestId } = await renderProvider();
      await waitFor(() => expect(mockGet).toHaveBeenCalled());
      expect(getByTestId('state').props.children).toBe('loading');

      await act(async () => {
        jest.advanceTimersByTime(SESSION_CHECK_TIMEOUT_MS);
      });
      await waitFor(() => expect(getByTestId('state').props.children).toBe('authed'));
    } finally {
      jest.useRealTimers();
    }
  });
});
