import React from 'react';
import { fireEvent, render } from '@testing-library/react-native';
import { Sentry } from '../../lib/sentry';

import { ErrorBoundary } from '../../components/ErrorBoundary';

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
});
