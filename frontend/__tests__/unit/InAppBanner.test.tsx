import React from 'react';
import { act, fireEvent, render } from '@testing-library/react-native';

import { InAppBanner } from '../../components/InAppBanner';
import { BannerProvider } from '../../contexts/BannerContext';
import { useBanner } from '../../hooks/useBanner';
import { AccessibilityInfo, Pressable, StyleSheet, Text } from 'react-native';

const mockOnSuccess = '#A1B2C3';
const mockOnError = '#D1E2F3';
const mockOnPrimary = '#E4F5A6';

let mockPathname = '/wishlist';
let mockSegments: string[] = ['(tabs)', 'wishlist'];
jest.mock('expo-router', () => ({
  usePathname: () => mockPathname,
  useSegments: () => mockSegments,
}));

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

let mockScreenReader = false;
let mockSrListener: ((on: boolean) => void) | null = null;

beforeEach(() => {
  jest.useFakeTimers();
  mockPathname = '/wishlist';
  mockSegments = ['(tabs)', 'wishlist'];
  mockScreenReader = false;
  mockSrListener = null;
  jest
    .spyOn(AccessibilityInfo, 'isScreenReaderEnabled')
    .mockImplementation(() => Promise.resolve(mockScreenReader));
  jest.spyOn(AccessibilityInfo, 'addEventListener').mockImplementation(((
    _e: string,
    cb: (on: boolean) => void
  ) => {
    mockSrListener = cb;
    return { remove: jest.fn() };
  }) as never);
});

afterEach(() => {
  jest.restoreAllMocks();
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

  describe('floating card position (#909, #936)', () => {
    it('sits below the safe-area inset plus header on a stack screen, inset 16px from the sides, not full-bleed', async () => {
      mockPathname = '/trash';
      mockSegments = ['trash'];
      const { getByTestId } = await renderWithBanner({ message: 'Book found', type: 'success' });
      await fireEvent.press(getByTestId('trigger'));
      const style = StyleSheet.flatten(getByTestId('in-app-banner').props.style);
      // Platform defaults to iOS in jest: inset 0 (mocked) + 44 header + 8 gap.
      expect(style.top).toBe(44 + 8);
      expect(style.left).toBe(16);
      expect(style.right).toBe(16);
      expect(style.borderRadius).toBeGreaterThan(0);
      expect(style.paddingTop).not.toBe(50);
    });

    it('sits just below the safe-area inset on a tab screen, which has no header (#936)', async () => {
      const { getByTestId } = await renderWithBanner({ message: 'Book found', type: 'success' });
      await fireEvent.press(getByTestId('trigger'));
      const style = StyleSheet.flatten(getByTestId('in-app-banner').props.style);
      // Inset 0 (mocked) + 8 gap, no header height.
      expect(style.top).toBe(0 + 8);
    });

    it('auto-dismisses a result banner with a View action, and View still fires', async () => {
      const onView = jest.fn();
      function Trigger() {
        const { showBanner } = useBanner();
        return (
          <Pressable
            testID="t2"
            onPress={() =>
              showBanner({
                message: 'Book found',
                type: 'success',
                actions: [{ label: 'View', onPress: onView }],
              })
            }
          >
            <Text>Show</Text>
          </Pressable>
        );
      }
      const { getByTestId, getByLabelText, queryByText } = await render(
        <BannerProvider>
          <InAppBanner />
          <Trigger />
        </BannerProvider>
      );
      // No explicit duration: the default timeout still clears it.
      await fireEvent.press(getByTestId('t2'));
      expect(queryByText('Book found')).toBeTruthy();
      await act(async () => {
        jest.advanceTimersByTime(10000 + 500);
      });
      expect(queryByText('Book found')).toBeNull();

      // View still fires while it is up.
      await fireEvent.press(getByTestId('t2'));
      await fireEvent.press(getByLabelText('View'));
      expect(onView).toHaveBeenCalledTimes(1);
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

  describe('persistence rule and route changes (#909)', () => {
    function Trigger({
      type,
      actions,
      duration,
    }: {
      type: 'success' | 'error' | 'info';
      actions?: { label: string; onPress: () => void }[];
      duration?: number;
    }) {
      const { showBanner } = useBanner();
      return (
        <Pressable
          testID="go"
          onPress={() => showBanner({ message: 'Msg', type, actions, duration })}
        >
          <Text>Show</Text>
        </Pressable>
      );
    }
    const view = [{ label: 'View', onPress: jest.fn() }];
    async function show(props: React.ComponentProps<typeof Trigger>) {
      const r = await render(
        <BannerProvider>
          <InAppBanner />
          <Trigger {...props} />
        </BannerProvider>
      );
      await act(async () => {}); // let the screen-reader query settle
      await fireEvent.press(r.getByTestId('go'));
      return r;
    }
    const advance = (ms: number) =>
      act(async () => {
        jest.advanceTimersByTime(ms);
      });

    it('clears when the active tab changes', async () => {
      const r = await show({ type: 'success', actions: view });
      expect(r.queryByText('Msg')).toBeTruthy();
      mockPathname = '/my-books';
      await r.rerender(
        <BannerProvider>
          <InAppBanner />
          <Trigger type="success" actions={view} />
        </BannerProvider>
      );
      await advance(500);
      expect(r.queryByText('Msg')).toBeNull();
    });

    it('clears an error banner with actions on a route change too', async () => {
      const r = await show({ type: 'error', actions: view });
      mockPathname = '/trash';
      await r.rerender(
        <BannerProvider>
          <InAppBanner />
          <Trigger type="error" actions={view} />
        </BannerProvider>
      );
      await advance(500);
      expect(r.queryByText('Msg')).toBeNull();
    });

    it('an action banner is still up at 6 s and gone after the 10 s fallback', async () => {
      const r = await show({ type: 'success', actions: view });
      await advance(6000);
      expect(r.queryByText('Msg')).toBeTruthy();
      await advance(4000 + 500);
      expect(r.queryByText('Msg')).toBeNull();
    });

    it('a banner without actions times out after 5 s', async () => {
      const r = await show({ type: 'info' });
      await advance(4000);
      expect(r.queryByText('Msg')).toBeTruthy();
      await advance(1000 + 500);
      expect(r.queryByText('Msg')).toBeNull();
    });

    it('honours an explicit duration on an action banner', async () => {
      const r = await show({ type: 'info', actions: view, duration: 2000 });
      await advance(2000 + 500);
      expect(r.queryByText('Msg')).toBeNull();
    });

    it('an error banner with actions stays past 10 s (screen reader off)', async () => {
      const r = await show({ type: 'error', actions: view });
      await advance(60000);
      expect(r.queryByText('Msg')).toBeTruthy();
    });

    it('an info/success action banner stays while a screen reader is on', async () => {
      mockScreenReader = true;
      const r = await show({ type: 'success', actions: view });
      await advance(60000);
      expect(r.queryByText('Msg')).toBeTruthy();
    });

    it('a screen reader turning on mid-banner stops the timeout', async () => {
      const r = await show({ type: 'success', actions: view });
      await advance(3000);
      await act(async () => mockSrListener?.(true));
      await advance(60000);
      expect(r.queryByText('Msg')).toBeTruthy();
    });

    it('without actions a banner still times out with a screen reader on', async () => {
      mockScreenReader = true;
      const r = await show({ type: 'info' });
      await advance(5000 + 500);
      expect(r.queryByText('Msg')).toBeNull();
    });

    it('on web a screen reader reported on does not hold an action banner past 10 s', async () => {
      const RN = require('react-native');
      const original = RN.Platform.OS;
      RN.Platform.OS = 'web';
      try {
        mockScreenReader = true;
        const r = await show({ type: 'success', actions: view });
        await advance(6000);
        expect(r.queryByText('Msg')).toBeTruthy();
        await advance(4000 + 500);
        expect(r.queryByText('Msg')).toBeNull();
      } finally {
        RN.Platform.OS = original;
      }
    });

    it('ignores a late isScreenReaderEnabled result after a change event arrived', async () => {
      let resolveQuery: (on: boolean) => void = () => {};
      (AccessibilityInfo.isScreenReaderEnabled as jest.Mock).mockImplementation(
        () => new Promise<boolean>((res) => (resolveQuery = res))
      );
      const r = await render(
        <BannerProvider>
          <InAppBanner />
          <Trigger type="success" actions={view} />
        </BannerProvider>
      );
      await act(async () => mockSrListener?.(false));
      await act(async () => resolveQuery(true)); // stale: must be ignored
      await fireEvent.press(r.getByTestId('go'));
      await advance(10000 + 500);
      expect(r.queryByText('Msg')).toBeNull();
    });

    it('sits just below the safe-area inset on the header-less login route', async () => {
      mockPathname = '/login';
      mockSegments = ['(auth)', 'login'];
      const r = await show({ type: 'info' });
      const style = StyleSheet.flatten(r.getByTestId('in-app-banner').props.style);
      expect(style.top).toBe(0 + 8);
    });
  });
});
