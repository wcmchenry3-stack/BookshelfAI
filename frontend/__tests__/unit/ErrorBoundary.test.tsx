import React from 'react';
import { fireEvent, render } from '@testing-library/react-native';
import { Sentry } from '../../lib/sentry';

import { ErrorBoundary } from '../../components/ErrorBoundary';
import { ThemeContext, lightTheme, darkTheme } from '../../theme';

jest.mock('@sentry/react-native', () => ({
  init: jest.fn(),
  addBreadcrumb: jest.fn(),
  captureException: jest.fn(),
  wrap: jest.fn((c: unknown) => c),
}));

function Bomb({ shouldThrow }: { shouldThrow: boolean }) {
  if (shouldThrow) throw new Error('test error');
  return null;
}

describe('ErrorBoundary', () => {
  let consoleError: jest.SpyInstance;

  beforeEach(() => {
    consoleError = jest.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    consoleError.mockRestore();
  });

  it('renders children when no error', async () => {
    const { getByText } = await render(
      <ErrorBoundary>
        <Bomb shouldThrow={false} />
      </ErrorBoundary>
    );
    expect(() => getByText('Something went wrong')).toThrow();
  });

  it('shows fallback UI when a child throws', async () => {
    const { getByText } = await render(
      <ErrorBoundary>
        <Bomb shouldThrow={true} />
      </ErrorBoundary>
    );
    expect(getByText('Something went wrong')).toBeTruthy();
    expect(getByText('The app ran into an unexpected error.')).toBeTruthy();
    expect(getByText('Try again')).toBeTruthy();
  });

  it('logs the error via componentDidCatch', async () => {
    await render(
      <ErrorBoundary>
        <Bomb shouldThrow={true} />
      </ErrorBoundary>
    );
    expect(consoleError).toHaveBeenCalledWith(
      '[ErrorBoundary] Unhandled error:',
      expect.any(Error),
      expect.anything()
    );
  });

  it('reports error to Sentry via captureException', async () => {
    await render(
      <ErrorBoundary>
        <Bomb shouldThrow={true} />
      </ErrorBoundary>
    );
    expect(Sentry.captureException).toHaveBeenCalledWith(expect.any(Error), {
      extra: { componentStack: expect.any(String) },
    });
  });

  it('resets error state when Try again is pressed', async () => {
    const { getByText, rerender } = await render(
      <ErrorBoundary>
        <Bomb shouldThrow={true} />
      </ErrorBoundary>
    );

    // Swap to non-throwing child first so the reset re-render succeeds
    await rerender(
      <ErrorBoundary>
        <Bomb shouldThrow={false} />
      </ErrorBoundary>
    );

    await fireEvent.press(getByText('Try again'));

    expect(() => getByText('Something went wrong')).toThrow();
  });

  describe('theme', () => {
    const renderIn = (theme: unknown) =>
      render(
        <ThemeContext.Provider value={theme as never}>
          <ErrorBoundary>
            <Bomb shouldThrow={true} />
          </ErrorBoundary>
        </ThemeContext.Provider>
      );

    it('renders the fallback with no ThemeProvider', async () => {
      const { getByText } = await render(
        <ErrorBoundary>
          <Bomb shouldThrow={true} />
        </ErrorBoundary>
      );
      expect(getByText('Something went wrong')).toBeTruthy();
      expect(getByText('Try again')).toBeTruthy();
    });

    it('renders the fallback when the theme context value is null', async () => {
      const { getByText } = await renderIn(null);
      expect(getByText('Something went wrong')).toBeTruthy();
    });

    it('renders the fallback when the theme has no colors', async () => {
      const { getByText } = await renderIn({ theme: {}, mode: 'light', toggleTheme: jest.fn() });
      expect(getByText('Try again')).toBeTruthy();
    });

    it.each([
      ['light', lightTheme],
      ['dark', darkTheme],
    ])('uses the same fixed error palette in %s mode', async (_m, theme) => {
      const { getByText } = await renderIn({ theme, mode: _m, toggleTheme: jest.fn() });
      const flat = (el: { props: Record<string, any> }) =>
        Object.assign({}, ...[el.props.style].flat(Infinity).filter(Boolean));
      expect(flat(getByText('Something went wrong')).color).toBe('#111');
      expect(flat(getByText('The app ran into an unexpected error.')).color).toBe('#555');
      expect(flat(getByText('Try again')).color).toBe('#fff');
    });
  });
});
