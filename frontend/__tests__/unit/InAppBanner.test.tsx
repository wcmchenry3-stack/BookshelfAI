import React from 'react';
import { act, fireEvent, render } from '@testing-library/react-native';

import { InAppBanner } from '../../components/InAppBanner';
import { BannerProvider } from '../../contexts/BannerContext';
import { useBanner } from '../../hooks/useBanner';
import { Pressable, StyleSheet, Text } from 'react-native';

const mockOnSuccess = '#A1B2C3';
const mockOnError = '#D1E2F3';
const mockOnPrimary = '#E4F5A6';

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: {
        background: '#fff',
        surface: '#f5f5f5',
        border: '#ccc',
        text: '#000',
        textSecondary: '#888',
        primary: '#007AFF',
        success: '#16A34A',
        error: '#DC2626',
        onSuccess: mockOnSuccess,
        onError: mockOnError,
        onPrimary: mockOnPrimary,
      },
      typography: { fontSizeBase: 16 },
    },
  }),
}));

function BannerTrigger({
  message,
  type,
  actions,
  numberOfLines,
}: {
  message: string;
  type: 'success' | 'error' | 'info';
  actions?: { label: string; onPress: () => void }[];
  numberOfLines?: number;
}) {
  const { showBanner } = useBanner();
  return (
    <Pressable
      testID="trigger"
      onPress={() => showBanner({ message, type, actions, duration: 10000, numberOfLines })}
    >
      <Text>Show</Text>
    </Pressable>
  );
}

async function renderWithBanner(triggerProps: {
  message: string;
  type: 'success' | 'error' | 'info';
  actions?: { label: string; onPress: () => void }[];
  numberOfLines?: number;
}) {
  return await render(
    <BannerProvider>
      <InAppBanner />
      <BannerTrigger {...triggerProps} />
    </BannerProvider>
  );
}

beforeEach(() => {
  jest.useFakeTimers();
});

afterEach(() => {
  jest.useRealTimers();
});

describe('InAppBanner', () => {
  it('renders nothing when no banner is shown', async () => {
    const { queryByRole } = await render(
      <BannerProvider>
        <InAppBanner />
      </BannerProvider>
    );
    expect(queryByRole('alert')).toBeNull();
  });

  it('renders message text when banner is triggered', async () => {
    const { getByTestId, getByText } = await renderWithBanner({
      message: 'Book found!',
      type: 'success',
    });
    await fireEvent.press(getByTestId('trigger'));
    expect(getByText('Book found!')).toBeTruthy();
  });

  it('truncates the message to 2 lines by default', async () => {
    const { getByTestId, getByText } = await renderWithBanner({ message: 'Long', type: 'error' });
    await fireEvent.press(getByTestId('trigger'));
    expect(getByText('Long').props.numberOfLines).toBe(2);
  });

  it('shows the whole message when numberOfLines is 0', async () => {
    const { getByTestId, getByText } = await renderWithBanner({
      message: 'Long',
      type: 'error',
      numberOfLines: 0,
    });
    await fireEvent.press(getByTestId('trigger'));
    expect(getByText('Long').props.numberOfLines).toBe(0);
  });

  it('renders action buttons when provided', async () => {
    const onPress = jest.fn();
    const { getByTestId, getByLabelText } = await renderWithBanner({
      message: 'Scan failed',
      type: 'error',
      actions: [{ label: 'Retry', onPress }],
    });
    await fireEvent.press(getByTestId('trigger'));
    const retryBtn = getByLabelText('Retry');
    expect(retryBtn).toBeTruthy();
  });

  it('calls action onPress when action button is tapped', async () => {
    const onPress = jest.fn();
    const { getByTestId, getByLabelText } = await renderWithBanner({
      message: 'Scan failed',
      type: 'error',
      actions: [{ label: 'Retry', onPress }],
    });
    await fireEvent.press(getByTestId('trigger'));
    await fireEvent.press(getByLabelText('Retry'));
    expect(onPress).toHaveBeenCalledTimes(1);
  });

  it('auto-dismisses after duration', async () => {
    const { getByTestId, getByText, queryByText } = await renderWithBanner({
      message: 'Done!',
      type: 'success',
    });
    await fireEvent.press(getByTestId('trigger'));
    expect(getByText('Done!')).toBeTruthy();

    // Advance past default + slide-out animation
    await act(async () => {
      jest.advanceTimersByTime(10000 + 500);
    });
    expect(queryByText('Done!')).toBeNull();
  });

  it('does not auto-dismiss a banner that carries an action', async () => {
    const onPress = jest.fn();
    const { getByTestId, getByText, getByLabelText } = await renderWithBanner({
      message: 'Not in our catalog',
      type: 'error',
      actions: [{ label: 'Add it yourself', onPress }],
    });
    await fireEvent.press(getByTestId('trigger'));

    // Well past the banner's own duration (10 s) and the slide-out.
    await act(async () => {
      jest.advanceTimersByTime(60000);
    });
    expect(getByText('Not in our catalog')).toBeTruthy();

    // The action is still there to use.
    await fireEvent.press(getByLabelText('Add it yourself'));
    expect(onPress).toHaveBeenCalledTimes(1);
  });

  describe('action button styling (#632)', () => {
    const cases: { type: 'success' | 'error' | 'info'; expected: string }[] = [
      { type: 'success', expected: mockOnSuccess },
      { type: 'error', expected: mockOnError },
      { type: 'info', expected: mockOnPrimary },
    ];

    it.each(cases)(
      'styles the action as a themed outline for type $type',
      async ({ type, expected }) => {
        const { getByTestId, getByLabelText } = await renderWithBanner({
          message: 'Message',
          type,
          actions: [{ label: 'Act', onPress: jest.fn() }],
        });
        await fireEvent.press(getByTestId('trigger'));
        const style = StyleSheet.flatten(getByLabelText('Act').props.style);

        expect(style.borderColor).toBe(expected);
        expect(style.borderWidth).toBe(1);
        expect(style.backgroundColor).toBeUndefined();
        expect(style.minWidth).toBe(44);
        expect(style.minHeight).toBe(44);
      }
    );

    it('keeps the accessibility role and label on the action button', async () => {
      const { getByTestId, getByLabelText } = await renderWithBanner({
        message: 'Message',
        type: 'error',
        actions: [{ label: 'Retry', onPress: jest.fn() }],
      });
      await fireEvent.press(getByTestId('trigger'));
      const button = getByLabelText('Retry');
      expect(button.props.accessibilityRole).toBe('button');
      expect(button.props.accessibilityLabel).toBe('Retry');
    });
  });

  it('dismisses on tap', async () => {
    const { getByTestId, getByText, queryByText } = await renderWithBanner({
      message: 'Tap to dismiss',
      type: 'info',
    });
    await fireEvent.press(getByTestId('trigger'));
    expect(getByText('Tap to dismiss')).toBeTruthy();

    // Tap the banner content to dismiss
    await fireEvent.press(getByText('Tap to dismiss'));

    // After slide-out animation
    await act(async () => {
      jest.advanceTimersByTime(500);
    });
    expect(queryByText('Tap to dismiss')).toBeNull();
  });
});
