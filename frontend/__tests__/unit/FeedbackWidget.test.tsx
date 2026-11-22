import React from 'react';
import { render, fireEvent, waitFor } from '@testing-library/react-native';
import { FeedbackWidget } from '../../components/FeedbackWidget/FeedbackWidget';
import { SessionLogger } from '../../components/FeedbackWidget/SessionLogger';
import { _resetFeedbackThrottle } from '../../components/FeedbackWidget/useFeedbackSubmit';
import { Sentry } from '../../lib/sentry';

jest.mock('../../lib/sentry', () => ({
  Sentry: { getClient: jest.fn(), captureFeedback: jest.fn() },
}));

const mockGetClient = Sentry.getClient as jest.Mock;
const mockCaptureFeedback = Sentry.captureFeedback as jest.Mock;

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: {
        surface: '#ffffff',
        onSurface: '#1a1a1a',
        onSurfaceVariant: '#666666',
        onPrimary: '#ffffff',
        primary: '#0f426f',
        outline: '#cccccc',
        surfaceContainerHigh: '#f5f5f5',
        error: '#c0392b',
        scrim: '#0000008c',
      },
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

// Records the props KeyboardAvoidingView receives (its host output does not expose `behavior`).
const mockKavProps: { behavior?: string }[] = [];
jest.mock('react-native/Libraries/Components/Keyboard/KeyboardAvoidingView', () => {
  const { View } = require('react-native');
  return {
    __esModule: true,
    default: (props: { behavior?: string; children?: React.ReactNode }) => {
      mockKavProps.push({ behavior: props.behavior });
      return <View>{props.children}</View>;
    },
  };
});

const DESC_PLACEHOLDER = "Describe what happened, or what you'd like to see...";

beforeEach(() => {
  mockGetClient.mockReset().mockReturnValue({});
  mockCaptureFeedback.mockReset();
  SessionLogger._reset();
  _resetFeedbackThrottle();
});

afterEach(() => {
  SessionLogger._reset();
});

async function renderWidget(
  opts: { visible?: boolean; onClose?: () => void; onSubmitted?: () => void } = {}
) {
  const { visible = true, onClose = jest.fn(), onSubmitted } = opts;
  return await render(
    <FeedbackWidget visible={visible} onClose={onClose} onSubmitted={onSubmitted} />
  );
}

async function fillAndSubmit(r: Awaited<ReturnType<typeof renderWidget>>, text = 'Description') {
  await fireEvent.changeText(r.getByPlaceholderText(DESC_PLACEHOLDER), text);
  await fireEvent.press(r.getByText('Submit'));
}

describe('FeedbackWidget', () => {
  describe('rendering', () => {
    it('renders the heading and the sheet body when visible', async () => {
      const { getByText } = await renderWidget();
      expect(getByText('Send Feedback')).toBeTruthy();
      expect(getByText('Description')).toBeTruthy();
    });

    it('renders type chips for Bug and Feature request', async () => {
      const { getByText } = await renderWidget();
      expect(getByText('Bug')).toBeTruthy();
      expect(getByText('Feature request')).toBeTruthy();
    });

    it('has only the description field (no title field)', async () => {
      const { getByPlaceholderText, queryByPlaceholderText, queryByText } = await renderWidget();
      expect(getByPlaceholderText(DESC_PLACEHOLDER)).toBeTruthy();
      expect(queryByPlaceholderText('Brief summary of the issue or idea')).toBeNull();
      expect(queryByText('Title')).toBeNull();
    });

    it('shows a character counter', async () => {
      const { getByText, getByPlaceholderText } = await renderWidget();
      expect(getByText('0/2000')).toBeTruthy();
      await fireEvent.changeText(getByPlaceholderText(DESC_PLACEHOLDER), 'abc');
      expect(getByText('3/2000')).toBeTruthy();
    });

    it('does not collapse the scroll body to zero height (blank sheet regression #756)', async () => {
      const { getByTestId } = await renderWidget();
      const { StyleSheet } = require('react-native');
      const flat = StyleSheet.flatten(getByTestId('feedback-body').props.style);
      expect(flat.flex).toBeUndefined();
      expect(flat.flexShrink).toBe(1);
    });

    it('gives the sheet a themed background', async () => {
      const { getByTestId } = await renderWidget();
      expect(getByTestId('feedback-sheet')).toHaveStyle({ backgroundColor: '#ffffff' });
    });
  });

  describe('close button', () => {
    it('calls onClose when pressed', async () => {
      const onClose = jest.fn();
      const { getByLabelText } = await renderWidget({ onClose });
      await fireEvent.press(getByLabelText('Close'));
      expect(onClose).toHaveBeenCalledTimes(1);
    });
  });

  describe('validation', () => {
    it('shows the description error when submitting an empty form', async () => {
      const { getByText } = await renderWidget();
      await fireEvent.press(getByText('Submit'));
      expect(getByText('Description is required.')).toBeTruthy();
      expect(mockCaptureFeedback).not.toHaveBeenCalled();
    });

    it('treats whitespace-only input as empty', async () => {
      const r = await renderWidget();
      await fillAndSubmit(r, '   ');
      expect(r.getByText('Description is required.')).toBeTruthy();
      expect(mockCaptureFeedback).not.toHaveBeenCalled();
    });
  });

  describe('successful submission', () => {
    it('sends to Sentry, closes itself and calls onSubmitted', async () => {
      const onClose = jest.fn();
      const onSubmitted = jest.fn();
      const r = await renderWidget({ onClose, onSubmitted });
      await fillAndSubmit(r, 'My description');

      await waitFor(() => expect(onSubmitted).toHaveBeenCalledTimes(1));
      expect(onClose).toHaveBeenCalledTimes(1);
      expect(mockCaptureFeedback).toHaveBeenCalledTimes(1);
      expect(mockCaptureFeedback.mock.calls[0][0].message).toBe('My description');
    });

    it('does not close or notify on failure', async () => {
      mockGetClient.mockReturnValue(undefined);
      const onClose = jest.fn();
      const onSubmitted = jest.fn();
      const r = await renderWidget({ onClose, onSubmitted });
      await fillAndSubmit(r);
      await waitFor(() =>
        expect(r.getByText('Something went wrong. Please try again.')).toBeTruthy()
      );
      expect(onClose).not.toHaveBeenCalled();
      expect(onSubmitted).not.toHaveBeenCalled();
    });
  });

  describe('error states', () => {
    it('shows the generic error when Sentry is not initialised', async () => {
      mockGetClient.mockReturnValue(undefined);
      const r = await renderWidget();
      await fillAndSubmit(r);
      await waitFor(() => {
        expect(r.getByText('Something went wrong. Please try again.')).toBeTruthy();
      });
    });

    it('shows the rate limit message after 5 submissions', async () => {
      // The sheet resets itself after each successful submit.
      const r = await renderWidget();
      for (let i = 0; i < 5; i++) await fillAndSubmit(r);
      expect(mockCaptureFeedback).toHaveBeenCalledTimes(5);

      await fillAndSubmit(r);
      await waitFor(() => {
        expect(r.getByText(/Too many submissions\. Please wait \d+ seconds/)).toBeTruthy();
      });
      expect(mockCaptureFeedback).toHaveBeenCalledTimes(5);
    });
  });

  describe('type selection', () => {
    it('switches to Feature request when chip is pressed', async () => {
      const { getByLabelText } = await renderWidget();
      const featureChip = getByLabelText('Feature request');
      await fireEvent.press(featureChip);
      expect(featureChip.props.accessibilityState?.selected).toBe(true);
    });
  });
});

describe('safe-area padding', () => {
  it('adds the bottom inset to the body padding so content clears the home indicator', async () => {
    const safeArea = require('react-native-safe-area-context');
    const spy = jest
      .spyOn(safeArea, 'useSafeAreaInsets')
      .mockReturnValue({ top: 0, bottom: 34, left: 0, right: 0 });
    try {
      const { getByTestId } = await renderWidget();
      const { StyleSheet } = require('react-native');
      const flat = StyleSheet.flatten(getByTestId('feedback-body').props.contentContainerStyle);
      expect(flat.paddingBottom).toBe(70);
    } finally {
      spy.mockRestore();
    }
  });
});

describe('keyboard avoidance', () => {
  const { Platform } = require('react-native');
  const original = Platform.OS;
  afterEach(() => {
    Platform.OS = original;
  });

  it('pads above the keyboard on iOS so the description field stays visible', async () => {
    Platform.OS = 'ios';
    mockKavProps.length = 0;
    await renderWidget();
    expect(mockKavProps.at(-1)?.behavior).toBe('padding');
  });

  it('leaves behavior unset on Android (the window resizes itself)', async () => {
    Platform.OS = 'android';
    mockKavProps.length = 0;
    await renderWidget();
    expect(mockKavProps.at(-1)?.behavior).toBeUndefined();
  });
});
