import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';
import { Platform } from 'react-native';

import type { EnrichedBook } from '../../components/BookCandidatePicker';
import type { ScanJobContextValue } from '../../contexts/ScanJobContext';
import { IOS_DISMISS_FALLBACK_MS } from '../../hooks/useModalHandoff';
import { useScanJobs } from '../../hooks/useScanJobs';

// #616 review fixes, through the real root layout (real provider, picker and
// sheet): "Add it yourself" must not discard the scan's other candidates, a
// mixed add outcome must keep the refused book reachable, and the picker and
// the form must never be presented at the same moment.

jest.mock('../../lib/sentry', () => ({
  Sentry: {
    wrap: (c: unknown) => c,
    addBreadcrumb: jest.fn(),
    captureException: jest.fn(),
    captureMessage: jest.fn(),
  },
}));

const mockPost = jest.fn();
jest.mock('../../lib/api', () => ({
  api: {
    get: jest.fn().mockResolvedValue({ data: {} }),
    post: (...a: unknown[]) => mockPost(...a),
    patch: jest.fn(),
    delete: jest.fn(),
  },
}));

jest.mock('../../lib/scanJobStorage', () => ({
  loadJobs: () => Promise.resolve([]),
  saveJobs: jest.fn().mockResolvedValue(undefined),
  clearJobs: jest.fn(),
  sweepOrphanedScanFiles: () => Promise.resolve(0),
  deleteScanImage: jest.fn(),
}));
jest.mock('@react-native-community/netinfo', () => ({
  __esModule: true,
  default: {
    fetch: () => Promise.resolve({ isConnected: true }),
    addEventListener: () => () => {},
  },
}));
jest.mock('expo-crypto', () => ({ randomUUID: () => 'uuid' }));
jest.mock('expo-router', () => {
  const { View } = require('react-native');
  const MockStack = ({ children }: { children?: React.ReactNode }) => <View>{children}</View>;
  MockStack.Screen = () => null;
  return { Stack: MockStack };
});
jest.mock('@expo/vector-icons', () => ({ MaterialIcons: () => null }));
jest.mock('expo-font', () => ({ useFonts: jest.fn(() => [true]) }));
jest.mock('@expo-google-fonts/noto-serif', () => ({
  NotoSerif_700Bold: 'a',
  NotoSerif_800ExtraBold: 'b',
}));
jest.mock('@expo-google-fonts/inter', () => ({
  Inter_400Regular: 'a',
  Inter_500Medium: 'b',
  Inter_600SemiBold: 'c',
  Inter_700Bold: 'd',
}));
jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      isDark: false,
      colors: new Proxy({}, { get: () => '#123456' }),
      typography: { fontSizeBase: 16, fontSizeSM: 14, fontSizeLG: 20, fontSizeXS: 12 },
    },
  }),
}));
jest.mock('../../theme', () => ({
  ThemeProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
jest.mock('../../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
jest.mock('../../components/ThemeToggleButton', () => ({ ThemeToggleButton: () => null }));
jest.mock('../../components/ErrorBoundary', () => ({
  ErrorBoundary: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

// Record every Modal render so the test can fire the iOS-only onDismiss.
type ModalProps = { onRequestClose?: unknown; onDismiss?: () => void };
let mockModalRenders: ModalProps[] = [];
jest.mock('react-native/Libraries/Modal/Modal', () => {
  const React = require('react');
  const Actual = jest.requireActual('@react-native/jest-preset/jest/mocks/Modal').default;
  return {
    __esModule: true,
    default: (props: ModalProps) => {
      mockModalRenders.push(props);
      return React.createElement(Actual, props);
    },
  };
});

// The layout renders <FeedbackButton /> inside the real ScanJobProvider: use
// it as the probe that hands the test the live context.
let ctx: ScanJobContextValue;
jest.mock('../../components/FeedbackWidget/FeedbackButton', () => ({
  FeedbackButton: () => {
    ctx = jest.requireActual('../../hooks/useScanJobs').useScanJobs();
    return null;
  },
}));

import RootLayout from '../../app/_layout';

void useScanJobs;

const book = (title: string, matched: boolean): EnrichedBook => ({
  ...(matched ? { open_library_work_id: `OL-${title}` } : {}),
  title,
  author: 'A. Author',
  subjects: [],
  confidence: 0.7,
  already_in_library: false,
  ownership: 'not_owned',
  editions: [{ isbn_13: '9780306406157' }],
  catalog_token: matched ? 'tok' : undefined,
});

const POND = book('Pond Notes', false);
const DUNE = book('Dune', true);
const EMMA = book('Emma', true);

let scanResults: EnrichedBook[] = [];
function seedJob(results: EnrichedBook[]) {
  scanResults = results;
}

async function open() {
  const utils = await render(<RootLayout />);
  await act(async () => {});
  mockPost.mockResolvedValueOnce({
    data: { books: scanResults, enhanced: false, enhanced_scans_remaining: null },
  });
  await act(async () => {
    await ctx.startScan('image', 'file:///scan.jpg');
  });
  await waitFor(() => expect(ctx.jobs[0]?.status).toBe('complete'));
  await act(() => ctx.reviewJob(ctx.jobs[0].id));
  return utils;
}

const setOS = (os: 'ios' | 'android') => {
  Object.defineProperty(Platform, 'OS', { configurable: true, get: () => os });
};
const realOS = Platform.OS;

beforeEach(() => {
  jest.clearAllMocks();
  mockPost.mockReset();
  mockModalRenders = [];
  jest.useFakeTimers();
  setOS('android');
});
afterEach(() => {
  jest.useRealTimers();
  setOS(realOS as 'ios');
});

/** Let the handoff finish. */
const settle = () => act(async () => void jest.advanceTimersByTime(IOS_DISMISS_FALLBACK_MS + 50));

describe('"Add it yourself" from the picker keeps the other candidates (#616)', () => {
  it('real _layout wiring: only that candidate leaves the job, the sheet opens, the picker returns', async () => {
    seedJob([POND, DUNE, EMMA]);
    const { getByLabelText, queryByLabelText, getByText, queryByText } = await open();
    expect(getByText('Pond Notes')).toBeTruthy();

    await fireEvent.press(getByLabelText('Add Pond Notes yourself'));
    await settle();

    // The job (and its photo) survives with the other two candidates.
    expect(ctx.jobs).toHaveLength(1);
    expect(ctx.jobs[0].imageUri).toBe('file:///scan.jpg');
    expect(ctx.jobs[0].results?.map((b) => b.title)).toEqual(['Dune', 'Emma']);
    // One sheet at a time: the form is up, the picker is not.
    expect(ctx.reviewingJob).toBeNull();
    expect(getByText('Add it yourself')).toBeTruthy();
    expect(queryByText('Dune')).toBeNull();

    // Closing the form reopens the review on what is left.
    await fireEvent.press(getByLabelText('Close form'));
    await settle();
    expect(ctx.unverifiedDrafts).toEqual([]);
    expect(ctx.reviewingJob?.id).toBe(ctx.jobs[0].id);
    expect(getByText('Dune')).toBeTruthy();
    expect(getByText('Emma')).toBeTruthy();
    expect(queryByLabelText('Add Pond Notes yourself')).toBeNull();
  });

  it('when it was the only candidate the job is discarded and nothing reopens', async () => {
    seedJob([POND]);
    const { getByLabelText } = await open();
    await fireEvent.press(getByLabelText('Add Pond Notes yourself'));
    await settle();
    expect(ctx.jobs).toEqual([]);
    expect(ctx.unverifiedDrafts).toHaveLength(1);
    await act(() => ctx.dismissUnverified());
    await settle();
    expect(ctx.reviewingJob).toBeNull();
  });
});

describe('mixed add outcome keeps the refused book reachable (#616)', () => {
  const answer = (t: string) =>
    t === 'Pond Notes'
      ? Promise.reject(
          Object.assign(new Error('422'), {
            response: { status: 422, data: { detail: 'book_not_in_catalog' } },
          })
        )
      : t === 'Dune'
        ? Promise.reject(Object.assign(new Error('500'), { response: { status: 500 } }))
        : Promise.resolve({ data: {} });

  it('keeps the not-in-catalog row (with its button) next to the retryable book', async () => {
    seedJob([POND, DUNE, EMMA]);
    const { getByLabelText } = await open();
    mockPost.mockImplementation((_u: string, b: { title: string }) => answer(b.title));
    await act(async () => {
      await ctx.handleAddBooks([POND, DUNE, EMMA]);
    });
    // Emma was added; Pond (refused) and Dune (500) stay in the open picker.
    expect(ctx.reviewingJob?.results?.map((b) => b.title)).toEqual(['Pond Notes', 'Dune']);
    expect(getByLabelText('Add Pond Notes yourself')).toBeTruthy();
  });

  it('the banner action closes the picker before opening the sheet, keeping Dune', async () => {
    seedJob([POND, DUNE, EMMA]);
    const { getByLabelText, getByText, queryByText } = await open();
    mockPost.mockImplementation((_u: string, b: { title: string }) => answer(b.title));
    await act(async () => {
      await ctx.handleAddBooks([POND, DUNE, EMMA]);
    });

    await fireEvent.press(getByLabelText('Add it yourself', { includeHiddenElements: true }));
    await settle();

    expect(ctx.reviewingJob).toBeNull();
    expect(ctx.jobs[0].results?.map((b) => b.title)).toEqual(['Dune']);
    expect(ctx.unverifiedDrafts.map((d) => d.title)).toEqual(['Pond Notes']);
    expect(getByText('Save')).toBeTruthy();
    expect(queryByText('Dune')).toBeNull();

    await act(() => ctx.dismissUnverified());
    await settle();
    expect(ctx.reviewingJob?.results?.map((b) => b.title)).toEqual(['Dune']);
  });
});

describe('one Modal at a time (#616)', () => {
  // Modals presented right now: the mock renders nothing for visible=false.
  const shownModals = (utils: { queryAllByRole: (r: string) => unknown[] }) =>
    utils.queryAllByRole('header').length;
  // The picker's Modal is the one whose onRequestClose is dismissReview.
  const pickerModalProps = () =>
    mockModalRenders.filter((p) => p.onRequestClose === ctx.dismissReview).at(-1)!;

  it('iOS: the sheet waits for the picker onDismiss, then opens', async () => {
    setOS('ios');
    seedJob([POND, DUNE]);
    const utils = await open();
    expect(shownModals(utils)).toBe(1);

    await fireEvent.press(utils.getByLabelText('Add Pond Notes yourself'));
    // Same commit as the picker closing: the form is not presented yet.
    expect(ctx.unverifiedDrafts).toHaveLength(1);
    expect(utils.queryByText('Save')).toBeNull();
    expect(shownModals(utils)).toBe(0);

    // The picker Modal reports it has finished dismissing.
    await act(async () => {
      pickerModalProps().onDismiss?.();
    });
    expect(utils.getByText('Save')).toBeTruthy();
    expect(shownModals(utils)).toBe(1);
  });

  it('iOS: falls back to a timer when onDismiss never fires', async () => {
    setOS('ios');
    seedJob([POND, DUNE]);
    const utils = await open();
    await fireEvent.press(utils.getByLabelText('Add Pond Notes yourself'));
    expect(utils.queryByText('Save')).toBeNull();
    await act(async () => void jest.advanceTimersByTime(IOS_DISMISS_FALLBACK_MS + 50));
    expect(utils.getByText('Save')).toBeTruthy();
  });

  it('Android and web: the sheet opens on the next tick, with no onDismiss', async () => {
    seedJob([POND, DUNE]);
    const utils = await open();
    await fireEvent.press(utils.getByLabelText('Add Pond Notes yourself'));
    expect(utils.queryByText('Save')).toBeNull();
    await settle();
    expect(utils.getByText('Save')).toBeTruthy();
  });
});
