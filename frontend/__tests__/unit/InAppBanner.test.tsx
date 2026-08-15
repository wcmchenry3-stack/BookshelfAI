import React from 'react';
import { act, fireEvent, render } from '@testing-library/react-native';

import { InAppBanner } from '../../components/InAppBanner';
import { BannerProvider } from '../../contexts/BannerContext';
import { useBanner } from '../../hooks/useBanner';
import { Pressable, Text } from 'react-native';

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
      },
      typography: { fontSizeBase: 16 },
    },
  }),
}));

function BannerTrigger({
  message,
  type,
  actions,
}: {
  message: string;
  type: 'success' | 'error' | 'info';
  actions?: { label: string; onPress: () => void }[];
}) {
  const { showBanner } = useBanner();
  return (
    <Pressable
      testID="trigger"
      onPress={() => showBanner({ message, type, actions, duration: 10000 })}
    >
      <Text>Show</Text>
    </Pressable>
  );
}

async function renderWithBanner(triggerProps: {
  message: string;
  type: 'success' | 'error' | 'info';
  actions?: { label: string; onPress: () => void }[];
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
