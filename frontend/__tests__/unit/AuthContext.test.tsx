import React, { useContext } from 'react';
import { Text } from 'react-native';
import { act, render, waitFor } from '@testing-library/react-native';

import { AuthContext, AuthProvider } from '../../contexts/AuthContext';

const mockPost = jest.fn();
jest.mock('../../lib/api', () => ({
  api: { post: (...args: unknown[]) => mockPost(...args) },
  ACCESS_TOKEN_KEY: 'bookshelf_access_token',
  REFRESH_TOKEN_KEY: 'bookshelf_refresh_token',
  setAuthFailureCallback: jest.fn(),
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

beforeEach(() => {
  jest.clearAllMocks();
  mockGetItem.mockResolvedValue(null);
  mockSetItem.mockResolvedValue(undefined);
  mockDeleteItem.mockResolvedValue(undefined);
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
