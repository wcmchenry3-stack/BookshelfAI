import React from 'react';
import { render, waitFor } from '@testing-library/react-native';

import Index from '../../app/index';

// ─── Mocks ───────────────────────────────────────────────────────────────────

const mockUseAuth = jest.fn();
jest.mock('../../hooks/useAuth', () => ({
  useAuth: () => mockUseAuth(),
}));

jest.mock('expo-router', () => ({
  Redirect: ({ href }: { href: string }) => {
    const { Text } = require('react-native');
    return <Text testID="redirect">{href}</Text>;
  },
}));

beforeEach(() => {
  jest.clearAllMocks();
});

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('Index (root redirect)', () => {
  it('shows loading indicator while auth state resolves', async () => {
    mockUseAuth.mockReturnValue({ isAuthenticated: false, isLoading: true });
    const { queryByTestId, UNSAFE_queryByType } = await render(<Index />);
    // Should NOT redirect while loading
    expect(queryByTestId('redirect')).toBeNull();
  });

  it('redirects to my-books when authenticated', async () => {
    mockUseAuth.mockReturnValue({ isAuthenticated: true, isLoading: false });
    const { getByTestId } = await render(<Index />);
    expect(getByTestId('redirect').props.children).toBe('/(tabs)/my-books');
  });

  it('redirects to login when not authenticated', async () => {
    mockUseAuth.mockReturnValue({ isAuthenticated: false, isLoading: false });
    const { getByTestId } = await render(<Index />);
    expect(getByTestId('redirect').props.children).toBe('/(auth)/login');
  });
});
