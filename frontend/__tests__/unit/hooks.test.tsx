import React from 'react';
import { renderHook } from '@testing-library/react-native';

import { useAuth } from '../../hooks/useAuth';
import { useTheme } from '../../hooks/useTheme';

const mockAuthValue = { user: null, signIn: jest.fn(), signOut: jest.fn() };
const mockThemeValue = { theme: {}, mode: 'light', toggleTheme: jest.fn() };

jest.mock('../../contexts/AuthContext', () => ({
  AuthContext: require('react').createContext(null),
}));

jest.mock('../../theme/ThemeContext', () => ({
  ThemeContext: require('react').createContext(null),
}));

describe('useAuth', () => {
  it('returns the AuthContext value', async () => {
    const { AuthContext } = require('../../contexts/AuthContext');
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <AuthContext.Provider value={mockAuthValue}>{children}</AuthContext.Provider>
    );
    const { result } = await renderHook(() => useAuth(), { wrapper });
    expect(result.current).toBe(mockAuthValue);
  });
});

describe('useTheme', () => {
  it('returns the ThemeContext value', async () => {
    const { ThemeContext } = require('../../theme/ThemeContext');
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <ThemeContext.Provider value={mockThemeValue}>{children}</ThemeContext.Provider>
    );
    const { result } = await renderHook(() => useTheme(), { wrapper });
    expect(result.current).toBe(mockThemeValue);
  });
});
