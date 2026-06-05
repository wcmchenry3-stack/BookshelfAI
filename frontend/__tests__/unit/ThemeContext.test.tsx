import React, { useContext } from 'react';
import { Text } from 'react-native';
import { act, fireEvent, render } from '@testing-library/react-native';

import { ThemeContext, ThemeProvider } from '../../theme/ThemeContext';
import { darkTheme, lightTheme } from '../../theme';

jest.mock('expo-secure-store', () => ({
  getItemAsync: jest.fn().mockResolvedValue(null),
  setItemAsync: jest.fn().mockResolvedValue(undefined),
}));

function ModeDisplay() {
  const { mode, theme, toggleTheme } = useContext(ThemeContext);
  return (
    <Text testID="mode" onPress={toggleTheme}>
      {mode}:{theme.isDark ? 'dark' : 'light'}
    </Text>
  );
}

describe('ThemeProvider', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    const SecureStore = require('expo-secure-store');
    SecureStore.getItemAsync.mockResolvedValue(null);
  });

  it('provides light mode by default (no system pref, no stored pref)', async () => {
    const { getByTestId } = render(
      <ThemeProvider>
        <ModeDisplay />
      </ThemeProvider>
    );
    await act(async () => {});
    expect(getByTestId('mode').props.children.join('')).toBe('light:light');
  });

  it('theme object matches lightTheme in light mode', async () => {
    let capturedTheme: typeof lightTheme | typeof darkTheme | null = null;
    function ThemeCapture() {
      const { theme } = useContext(ThemeContext);
      // eslint-disable-next-line react-hooks/globals
      capturedTheme = theme;
      return null;
    }
    render(
      <ThemeProvider>
        <ThemeCapture />
      </ThemeProvider>
    );
    await act(async () => {});
    expect(capturedTheme).toEqual(lightTheme);
  });

  it('toggleTheme switches from light to dark', async () => {
    const { getByTestId } = render(
      <ThemeProvider>
        <ModeDisplay />
      </ThemeProvider>
    );
    await act(async () => {});
    fireEvent.press(getByTestId('mode'));
    expect(getByTestId('mode').props.children.join('')).toBe('dark:dark');
  });

  it('toggleTheme persists preference to SecureStore', async () => {
    const SecureStore = require('expo-secure-store');
    const { getByTestId } = render(
      <ThemeProvider>
        <ModeDisplay />
      </ThemeProvider>
    );
    await act(async () => {});
    fireEvent.press(getByTestId('mode'));
    expect(SecureStore.setItemAsync).toHaveBeenCalledWith('bookshelf_theme_preference', 'dark');
  });

  it('loads persisted dark preference from SecureStore on mount', async () => {
    const SecureStore = require('expo-secure-store');
    SecureStore.getItemAsync.mockResolvedValueOnce('dark');
    const { getByTestId } = render(
      <ThemeProvider>
        <ModeDisplay />
      </ThemeProvider>
    );
    await act(async () => {});
    expect(getByTestId('mode').props.children.join('')).toBe('dark:dark');
  });

  it('ignores invalid stored preference values', async () => {
    const SecureStore = require('expo-secure-store');
    SecureStore.getItemAsync.mockResolvedValueOnce('invalid-value');
    const { getByTestId } = render(
      <ThemeProvider>
        <ModeDisplay />
      </ThemeProvider>
    );
    await act(async () => {});
    // Invalid value is ignored; stays at default light
    expect(getByTestId('mode').props.children.join('')).toBe('light:light');
  });
});
