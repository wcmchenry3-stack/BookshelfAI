import React from 'react';
import { render, fireEvent, act } from '@testing-library/react-native';
import { FeedbackProvider } from '../../components/FeedbackWidget/FeedbackContext';
import { HeaderFeedbackButton } from '../../components/FeedbackWidget/HeaderFeedbackButton';
import SettingsScreen from '../../app/(tabs)/settings';
import { Sentry } from '../../lib/sentry';

jest.mock('../../lib/sentry', () => ({
  Sentry: { getClient: jest.fn(), captureFeedback: jest.fn(), addBreadcrumb: jest.fn() },
}));

jest.mock('../../hooks/useAuth', () => ({ useAuth: () => ({ logout: jest.fn() }) }));
jest.mock('../../components/ThemeToggleButton', () => ({ ThemeToggleButton: () => null }));

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: {
        background: '#fff',
        text: '#000',
        textSecondary: '#555',
        error: '#c00',
        surface: '#ffffff',
        inverseSurface: '#222',
        inverseOnSurface: '#eee',
        onSurface: '#1a1a1a',
        onSurfaceVariant: '#666666',
        onPrimary: '#ffffff',
        primary: '#0f426f',
        outline: '#cccccc',
        surfaceContainerHigh: '#f5f5f5',
        scrim: '#0000008c',
      },
      spacing: { md: 16 },
      typography: {
        fontFamilyBody: 'Inter_400Regular',
        fontSizeLG: 20,
        fontSizeBase: 16,
        fontSizeSM: 14,
        fontSizeXS: 12,
      },
    },
  }),
}));

beforeEach(() => {
  (Sentry.getClient as jest.Mock).mockReset().mockReturnValue({});
  (Sentry.captureFeedback as jest.Mock).mockReset();
});

describe('feedback entry points', () => {
  it('header "?" button opens the widget', async () => {
    const { getByLabelText, queryByText, getByText } = await render(
      <FeedbackProvider>
        <HeaderFeedbackButton />
      </FeedbackProvider>
    );
    expect(queryByText('Send Feedback')).toBeNull();
    await fireEvent.press(getByLabelText('Send feedback'));
    expect(getByText('Send Feedback')).toBeTruthy();
  });

  it('header button has a >=44pt hit target including hitSlop', async () => {
    const { getByTestId } = await render(
      <FeedbackProvider>
        <HeaderFeedbackButton />
      </FeedbackProvider>
    );
    const btn = getByTestId('header-feedback-button');
    const { StyleSheet } = require('react-native');
    const flat = StyleSheet.flatten(btn.props.style);
    const slack = typeof btn.props.hitSlop === 'number' ? btn.props.hitSlop : 0;
    expect(flat.width + slack * 2).toBeGreaterThanOrEqual(44);
    expect(flat.height + slack * 2).toBeGreaterThanOrEqual(44);
    expect(btn.props.accessibilityRole).toBe('button');
  });

  it('Settings "Send feedback" row opens the widget', async () => {
    const { getByTestId, queryByLabelText, getByLabelText } = await render(
      <FeedbackProvider>
        <SettingsScreen />
      </FeedbackProvider>
    );
    expect(queryByLabelText('Close')).toBeNull();
    await fireEvent.press(getByTestId('settings-feedback'));
    expect(getByLabelText('Close')).toBeTruthy();
  });

  it('shows the thanks banner after a submit from any entry point', async () => {
    const { getByTestId, getByText, getByPlaceholderText, queryByTestId } = await render(
      <FeedbackProvider>
        <SettingsScreen />
      </FeedbackProvider>
    );
    await fireEvent.press(getByTestId('settings-feedback'));
    await fireEvent.changeText(getByPlaceholderText(/Describe what happened/), 'It broke');
    await act(async () => {
      await fireEvent.press(getByText('Submit'));
    });
    expect(queryByTestId('feedback-thanks-banner')).toBeTruthy();
  });
});
