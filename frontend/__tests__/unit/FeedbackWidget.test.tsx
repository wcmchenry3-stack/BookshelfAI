import React from 'react';
import { render, fireEvent, act, waitFor } from '@testing-library/react-native';
import { FeedbackWidget } from '../../components/FeedbackWidget/FeedbackWidget';
import { SessionLogger } from '../../components/FeedbackWidget/SessionLogger';

const mockFetch = jest.fn() as jest.MockedFunction<typeof fetch>;
global.fetch = mockFetch;

const WORKER_URL = 'https://feedback-worker.wcmchenry3.workers.dev';

// Mock useTheme to avoid ThemeContext setup
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
        outlineVariant: '#e0e0e0',
        surfaceContainerHigh: '#f5f5f5',
        error: '#c0392b',
        errorContainer: '#fdd',
        onErrorContainer: '#c0392b',
      },
      typography: {
        fontSizeLG: 20,
        fontSizeBase: 16,
        fontSizeSM: 14,
        fontSizeXS: 12,
      },
      spacing: { xs: 4, sm: 8, md: 16, lg: 24 },
    },
  }),
}));

beforeEach(() => {
  mockFetch.mockReset();
  SessionLogger._reset();
  process.env.EXPO_PUBLIC_FEEDBACK_WORKER_URL = WORKER_URL;
});

afterEach(() => {
  delete process.env.EXPO_PUBLIC_FEEDBACK_WORKER_URL;
  SessionLogger._reset();
});

async function renderWidget(opts: { visible?: boolean; onClose?: () => void } = {}) {
  const { visible = true, onClose = jest.fn() } = opts;
  return await render(<FeedbackWidget visible={visible} onClose={onClose} />);
}

describe('FeedbackWidget', () => {
  describe('rendering', () => {
    it('renders the heading when visible', async () => {
      const { getByText } = await renderWidget();
      expect(getByText('Send Feedback')).toBeTruthy();
    });

    it('renders type chips for Bug and Feature request', async () => {
      const { getByText } = await renderWidget();
      expect(getByText('Bug')).toBeTruthy();
      expect(getByText('Feature request')).toBeTruthy();
    });

    it('renders title and description placeholders', async () => {
      const { getByPlaceholderText } = await renderWidget();
      expect(getByPlaceholderText('Brief summary of the issue or idea')).toBeTruthy();
      expect(
        getByPlaceholderText("Describe what happened, or what you'd like to see...")
      ).toBeTruthy();
    });

    it('renders the Submit button', async () => {
      const { getByText } = await renderWidget();
      expect(getByText('Submit')).toBeTruthy();
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
    it('shows title error when submitting empty form', async () => {
      const { getByText } = await renderWidget();
      await act(async () => {
        await fireEvent.press(getByText('Submit'));
      });
      expect(getByText('Title is required.')).toBeTruthy();
      expect(mockFetch).not.toHaveBeenCalled();
    });

    it('shows description error when title filled but description empty', async () => {
      const { getByText, getByPlaceholderText } = await renderWidget();
      await fireEvent.changeText(
        getByPlaceholderText('Brief summary of the issue or idea'),
        'A title'
      );
      await act(async () => {
        await fireEvent.press(getByText('Submit'));
      });
      expect(getByText('Description is required.')).toBeTruthy();
      expect(mockFetch).not.toHaveBeenCalled();
    });
  });

  describe('successful submission', () => {
    it('shows success message after 201 response', async () => {
      mockFetch.mockResolvedValueOnce({
        status: 201,
        json: async () => ({ issueNumber: 12, issueUrl: 'https://github.com/issues/12' }),
        headers: { get: () => null },
      } as unknown as Response);

      const { getByText, getByPlaceholderText } = await renderWidget();
      await fireEvent.changeText(
        getByPlaceholderText('Brief summary of the issue or idea'),
        'My title'
      );
      await fireEvent.changeText(
        getByPlaceholderText("Describe what happened, or what you'd like to see..."),
        'My description'
      );
      await act(async () => {
        await fireEvent.press(getByText('Submit'));
      });
      await waitFor(() => {
        expect(getByText('Thanks for your feedback!')).toBeTruthy();
      });
      expect(getByText('Your report was filed as issue #12.')).toBeTruthy();
    });
  });

  describe('error states', () => {
    it('shows rate limit message on 429', async () => {
      mockFetch.mockResolvedValueOnce({
        status: 429,
        headers: { get: (h: string) => (h === 'Retry-After' ? '60' : null) },
        json: async () => ({}),
      } as unknown as Response);

      const { getByText, getByPlaceholderText } = await renderWidget();
      await fireEvent.changeText(
        getByPlaceholderText('Brief summary of the issue or idea'),
        'Title'
      );
      await fireEvent.changeText(
        getByPlaceholderText("Describe what happened, or what you'd like to see..."),
        'Description'
      );
      await act(async () => {
        await fireEvent.press(getByText('Submit'));
      });
      await waitFor(() => {
        expect(getByText(/Too many submissions/)).toBeTruthy();
      });
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
