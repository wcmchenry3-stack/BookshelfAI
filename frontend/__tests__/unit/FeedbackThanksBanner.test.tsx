import React from 'react';
import { AccessibilityInfo, Platform } from 'react-native';
import { render, act } from '@testing-library/react-native';
import {
  FeedbackThanksBanner,
  THANKS_BANNER_DURATION_MS,
} from '../../components/FeedbackWidget/FeedbackThanksBanner';

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: { inverseSurface: '#222222', inverseOnSurface: '#eeeeee' },
      typography: { fontFamilyBody: 'Inter_400Regular', fontSizeSM: 14 },
    },
  }),
}));

afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
});

describe('FeedbackThanksBanner', () => {
  it('renders nothing when not visible', async () => {
    const { queryByTestId } = await render(
      <FeedbackThanksBanner visible={false} onDismiss={jest.fn()} />
    );
    expect(queryByTestId('feedback-thanks-banner')).toBeNull();
  });

  it('shows the success message, non-interactive, as a polite live region', async () => {
    const { getByTestId, getByText } = await render(
      <FeedbackThanksBanner visible onDismiss={jest.fn()} />
    );
    expect(getByText('Thanks for your feedback!')).toBeTruthy();
    const banner = getByTestId('feedback-thanks-banner');
    expect(banner.props.pointerEvents).toBe('none');
    expect(banner.props.accessibilityLiveRegion).toBe('polite');
  });

  it('auto-dismisses after 4 seconds', async () => {
    jest.useFakeTimers();
    const onDismiss = jest.fn();
    await render(<FeedbackThanksBanner visible onDismiss={onDismiss} />);

    await act(async () => {
      jest.advanceTimersByTime(THANKS_BANNER_DURATION_MS - 1);
    });
    expect(onDismiss).not.toHaveBeenCalled();

    await act(async () => {
      jest.advanceTimersByTime(1);
    });
    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(THANKS_BANNER_DURATION_MS).toBe(4000);
  });

  it('announces explicitly on iOS only', async () => {
    const announce = jest.spyOn(AccessibilityInfo, 'announceForAccessibility');
    const original = Platform.OS;
    try {
      Platform.OS = 'ios';
      await render(<FeedbackThanksBanner visible onDismiss={jest.fn()} />);
      expect(announce).toHaveBeenCalledWith('Thanks for your feedback!');

      announce.mockClear();
      Platform.OS = 'android';
      await render(<FeedbackThanksBanner visible onDismiss={jest.fn()} />);
      expect(announce).not.toHaveBeenCalled();
    } finally {
      Platform.OS = original;
    }
  });
});
