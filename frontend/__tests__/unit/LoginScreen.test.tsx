import React from 'react';
import { Alert } from 'react-native';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

import LoginScreen from '../../app/(auth)/login';

// ─── Mocks ───────────────────────────────────────────────────────────────────

const mockLogin = jest.fn();
const mockTestLogin = jest.fn();
jest.mock('../../hooks/useAuth', () => ({
  useAuth: () => ({ login: mockLogin, testLogin: mockTestLogin }),
}));

jest.mock('../../lib/api', () => ({
  api: { post: jest.fn() },
  ACCESS_TOKEN_KEY: 'bookshelf_access_token',
  REFRESH_TOKEN_KEY: 'bookshelf_refresh_token',
}));

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: {
        background: '#fbf9f5',
        surface: '#fbf9f5',
        text: '#1b1c1a',
        primary: '#0f426f',
        onSurface: '#1b1c1a',
        onSurfaceVariant: '#42474f',
        secondaryContainer: '#c6e7dd',
        onSecondaryContainer: '#4b6861',
      },
      typography: {
        fontSizeXL: 24,
        fontWeightBold: '700',
        fontFamilyHeadline: 'NotoSerif_700Bold',
      },
      spacing: { xl: 32, md: 16 },
    },
  }),
}));

jest.mock('expo-web-browser', () => ({
  maybeCompleteAuthSession: jest.fn(),
}));

const mockPromptAsync = jest.fn();

jest.mock('expo-auth-session/providers/google', () => ({
  useIdTokenAuthRequest: jest.fn(),
}));

function setupGoogleAuth(opts: { request?: object | null; response?: object | null } = {}) {
  const { useIdTokenAuthRequest } = require('expo-auth-session/providers/google');
  // Use 'in' check so callers can explicitly pass null to simulate no request yet
  const requestValue = 'request' in opts ? opts.request : { clientId: 'test' };
  const responseValue = 'response' in opts ? opts.response : null;
  useIdTokenAuthRequest.mockReturnValue([requestValue, responseValue, mockPromptAsync]);
}

beforeEach(() => {
  jest.clearAllMocks();
  setupGoogleAuth();
});

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('LoginScreen', () => {
  it('renders the app title', async () => {
    const { getByText } = await render(<LoginScreen />);
    expect(getByText('BookshelfAI')).toBeTruthy();
  });

  it('renders Sign in button when request is ready', async () => {
    const { getByLabelText } = await render(<LoginScreen />);
    expect(getByLabelText('Sign in with Google')).toBeTruthy();
  });

  it('shows Sign in text when request is truthy', async () => {
    const { getByText } = await render(<LoginScreen />);
    expect(getByText('Sign in with Google')).toBeTruthy();
  });

  it('shows ActivityIndicator when request is not yet ready', async () => {
    setupGoogleAuth({ request: null });
    const { queryByText, getByLabelText } = await render(<LoginScreen />);
    // Button label is still present (for accessibility) but text content is a spinner
    expect(queryByText('Sign in with Google')).toBeNull();
    expect(getByLabelText('Sign in with Google')).toBeTruthy();
  });

  it('button is disabled when request is null', async () => {
    setupGoogleAuth({ request: null });
    const { getByLabelText } = await render(<LoginScreen />);
    const btn = getByLabelText('Sign in with Google');
    expect(btn.props.accessibilityState?.disabled ?? btn.props.disabled).toBeTruthy();
  });

  it('calls promptAsync when button pressed', async () => {
    const { getByLabelText } = await render(<LoginScreen />);
    await fireEvent.press(getByLabelText('Sign in with Google'));
    expect(mockPromptAsync).toHaveBeenCalled();
  });

  it('calls login with id_token when Google response is success', async () => {
    setupGoogleAuth({
      response: { type: 'success', params: { id_token: 'google-id-token-abc' } },
    });
    mockLogin.mockResolvedValue(undefined);
    await render(<LoginScreen />);
    await waitFor(() => expect(mockLogin).toHaveBeenCalledWith('google-id-token-abc'));
  });

  it('does not call login when response type is not success', async () => {
    setupGoogleAuth({ response: { type: 'dismiss' } });
    await render(<LoginScreen />);
    await waitFor(() => expect(mockLogin).not.toHaveBeenCalled());
  });

  it('does not call login when response is null', async () => {
    setupGoogleAuth({ response: null });
    await render(<LoginScreen />);
    await waitFor(() => expect(mockLogin).not.toHaveBeenCalled());
  });

  it('shows error alert when login fails', async () => {
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    setupGoogleAuth({
      response: { type: 'success', params: { id_token: 'bad-token' } },
    });
    mockLogin.mockRejectedValue(new Error('401 Unauthorized'));
    await render(<LoginScreen />);
    await waitFor(() =>
      expect(alertSpy).toHaveBeenCalledWith('Error', 'Sign-in failed. Please try again.')
    );
    alertSpy.mockRestore();
  });
});
