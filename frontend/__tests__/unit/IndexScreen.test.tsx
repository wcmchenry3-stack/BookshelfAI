import React from 'react';
import { render } from '@testing-library/react-native';

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
    const { queryByTestId } = await render(<Index />);
    // Should NOT redirect while loading
    expect(queryByTestId('redirect')).toBeNull();
  });

  it('redirects to the Scan tab when authenticated (#904)', async () => {
    mockUseAuth.mockReturnValue({ isAuthenticated: true, isLoading: false });
    const { getByTestId } = await render(<Index />);
    expect(getByTestId('redirect').props.children).toBe('/(tabs)/scan');
  });

  it.each([
    ['scan', '/(tabs)/scan'],
    ['wishlist', '/(tabs)/wishlist'],
    ['my-books', '/(tabs)/my-books'],
  ])('redirects to the user\'s "Open on" tab %s (#913)', async (tab, route) => {
    mockUseAuth.mockReturnValue({ isAuthenticated: true, isLoading: false, defaultTab: tab });
    const { getByTestId } = await render(<Index />);
    expect(getByTestId('redirect').props.children).toBe(route);
  });

  it.each([null, 'settings', 42])('redirects to Scan for an unknown tab %p (#913)', async (tab) => {
    mockUseAuth.mockReturnValue({ isAuthenticated: true, isLoading: false, defaultTab: tab });
    const { getByTestId } = await render(<Index />);
    expect(getByTestId('redirect').props.children).toBe('/(tabs)/scan');
  });

  it('redirects to login when not authenticated', async () => {
    mockUseAuth.mockReturnValue({ isAuthenticated: false, isLoading: false });
    const { getByTestId } = await render(<Index />);
    expect(getByTestId('redirect').props.children).toBe('/(auth)/login');
  });
});
