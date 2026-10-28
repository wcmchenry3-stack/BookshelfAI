import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';
import { Platform } from 'react-native';

import { AddUnverifiedBookSheet } from '../../components/AddUnverifiedBookSheet';
import type { EnrichedBook } from '../../components/BookCandidatePicker';
import type { ScanJobContextValue } from '../../contexts/ScanJobContext';
import { IOS_DISMISS_FALLBACK_MS } from '../../hooks/useModalHandoff';
import { draftFromBook, scanReferenceOf, withScanRecord } from '../../lib/unverified';

// #631: an "Add it yourself" save pre-filled from a photo scan names the
// scan candidate it came from ({scan_record_id, candidate_index}, never the
// pre-filled values), so the server can tell whether the user corrected
// the reading. A form with no scan behind it sends no reference. The
// reference rides on each result from the POST /scan response, through the
// job (a retried one too) and the draft queue (draft ids, #625), to the save.

jest.mock('../../hooks/useAuth', () => ({ useAuth: () => ({ isAuthenticated: true }) }));
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

const REC = '7c9e6679-7425-40de-944b-e07fc1f90ae7';

const book = (title: string, index: number | null): EnrichedBook => ({
  title,
  author: 'A. Author',
  subjects: [],
  confidence: 0.7,
  already_in_library: false,
  ownership: 'not_owned',
  editions: [{}],
  candidate_index: index,
});

const POND = book('Pond Notes', 0);
const MARSH = book('Marsh Log', 2);

const scanResponse = (books: EnrichedBook[], scanRecordId?: string) => ({
  data: {
    books,
    enhanced: false,
    enhanced_scans_remaining: null,
    ...(scanRecordId ? { scan_record_id: scanRecordId } : {}),
  },
});

const saved = (title: string) => ({
  data: { id: `ub-${title}`, unverified: true, book: { id: 'c', title, author: 'A' } },
});

const setOS = (os: 'ios' | 'android') => {
  Object.defineProperty(Platform, 'OS', { configurable: true, get: () => os });
};
const realOS = Platform.OS;

beforeEach(() => {
  jest.clearAllMocks();
  mockPost.mockReset();
  jest.useFakeTimers();
  setOS('android');
});
afterEach(() => {
  jest.useRealTimers();
  setOS(realOS as 'ios');
});

const settle = () => act(async () => void jest.advanceTimersByTime(IOS_DISMISS_FALLBACK_MS + 50));

async function scanAndReview(books: EnrichedBook[], scanRecordId?: string) {
  const utils = await render(<RootLayout />);
  await act(async () => {});
  mockPost.mockResolvedValueOnce(scanResponse(books, scanRecordId));
  await act(async () => {
    await ctx.startScan('image', 'file:///scan.jpg');
  });
  await waitFor(() => expect(ctx.jobs[0]?.status).toBe('complete'));
  await act(() => ctx.reviewJob(ctx.jobs[0].id));
  return utils;
}

function unverifiedPosts() {
  return mockPost.mock.calls.filter((c) => c[0] === '/wishlist/unverified').map((c) => c[1]);
}

describe('lib/unverified scan reference (#631)', () => {
  it('copies the response record id onto each result with a candidate index', () => {
    const books = withScanRecord([POND, book('No index', null)], REC);
    expect(books[0]).toEqual({ ...POND, scan_record_id: REC });
    expect(books[1].scan_record_id).toBeUndefined();
  });

  it('leaves the results alone when the response has no record id', () => {
    const books = [POND];
    expect(withScanRecord(books, undefined)).toBe(books);
    expect(withScanRecord(books, null)).toBe(books);
    expect(withScanRecord(books, '')).toBe(books);
  });

  it('puts the reference on the draft only when both parts are valid', () => {
    expect(draftFromBook({ ...POND, scan_record_id: REC }).scanReference).toEqual({
      scan_record_id: REC,
      candidate_index: 0,
    });
    expect('scanReference' in draftFromBook(POND)).toBe(false);
    expect(scanReferenceOf({ ...POND, scan_record_id: REC, candidate_index: -1 })).toBeUndefined();
    expect(scanReferenceOf({ ...POND, scan_record_id: REC, candidate_index: 1.5 })).toBeUndefined();
    expect(
      scanReferenceOf({ ...POND, scan_record_id: REC, candidate_index: null })
    ).toBeUndefined();
  });
});

describe('AddUnverifiedBookSheet sends the reference (#631)', () => {
  async function saveSheet(draft: ReturnType<typeof draftFromBook>) {
    mockPost.mockResolvedValueOnce(saved(draft.title));
    const onSaved = jest.fn();
    const utils = await render(
      <AddUnverifiedBookSheet
        visible
        draft={draft}
        draftKey="k1"
        onSaved={onSaved}
        onDismiss={jest.fn()}
      />
    );
    await fireEvent.changeText(utils.getByTestId('unverified-author-input'), 'Someone Else');
    await fireEvent.press(utils.getByLabelText('Save'));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
  }

  it('on a save pre-filled from a scan result, with the values as typed', async () => {
    await saveSheet(draftFromBook({ ...POND, scan_record_id: REC }));
    expect(unverifiedPosts()).toEqual([
      {
        title: 'Pond Notes',
        author: 'Someone Else',
        isbn: null,
        scan_reference: { scan_record_id: REC, candidate_index: 0 },
      },
    ]);
  });

  it('not on a manual add with no scan behind it', async () => {
    await saveSheet({ title: 'Typed', author: 'Me', isbn: '' });
    expect(unverifiedPosts()).toEqual([{ title: 'Typed', author: 'Someone Else', isbn: null }]);
  });
});

describe('the reference survives the job and the draft handoff (#631)', () => {
  it('a scan result opened in the form sends its own reference', async () => {
    const utils = await scanAndReview([POND, MARSH], REC);
    expect(ctx.jobs[0].results?.map((b) => b.scan_record_id)).toEqual([REC, REC]);

    await fireEvent.press(utils.getByLabelText('Add Marsh Log yourself'));
    await settle();
    expect(ctx.unverifiedDrafts.map((d) => d.scanReference)).toEqual([
      { scan_record_id: REC, candidate_index: 2 },
    ]);
    mockPost.mockResolvedValueOnce(saved('Marsh Log'));
    await fireEvent.press(utils.getByLabelText('Save'));
    await settle();

    expect(unverifiedPosts()).toEqual([
      {
        title: 'Marsh Log',
        author: 'A. Author',
        isbn: null,
        scan_reference: { scan_record_id: REC, candidate_index: 2 },
      },
    ]);
  });

  it('queued drafts: each form in the queue sends its own candidate', async () => {
    const utils = await scanAndReview([POND, MARSH], REC);
    await act(() => ctx.addYourself(ctx.jobs[0].results ?? []));
    await settle();
    expect(ctx.unverifiedDrafts.map((d) => [d.id, d.scanReference?.candidate_index])).toEqual([
      [expect.any(String), 0],
      [expect.any(String), 2],
    ]);

    mockPost.mockResolvedValueOnce(saved('Pond Notes'));
    await fireEvent.press(utils.getByLabelText('Save'));
    await settle();
    // The next draft resets the same form in place (draftKey); its save
    // names its own candidate, not the first one's.
    expect(utils.getByTestId('unverified-title-input').props.value).toBe('Marsh Log');
    mockPost.mockResolvedValueOnce(saved('Marsh Log'));
    await fireEvent.press(utils.getByLabelText('Save'));
    await settle();

    expect(unverifiedPosts().map((b) => b.scan_reference)).toEqual([
      { scan_record_id: REC, candidate_index: 0 },
      { scan_record_id: REC, candidate_index: 2 },
    ]);
  });

  it('a retried job keeps the reference from the answer that completed it', async () => {
    const utils = await render(<RootLayout />);
    await act(async () => {});
    mockPost.mockRejectedValueOnce(new Error('Network Error'));
    await act(async () => {
      await ctx.startScan('image', 'file:///scan.jpg');
    });
    await waitFor(() => expect(ctx.jobs[0]?.status).toBe('failed'));

    mockPost.mockResolvedValueOnce(scanResponse([POND], REC));
    await act(async () => {
      await ctx.retryScan(ctx.jobs[0].id);
    });
    await waitFor(() => expect(ctx.jobs[0]?.status).toBe('complete'));
    await act(() => ctx.reviewJob(ctx.jobs[0].id));
    await fireEvent.press(utils.getByLabelText('Add Pond Notes yourself'));
    await settle();
    mockPost.mockResolvedValueOnce(saved('Pond Notes'));
    await fireEvent.press(utils.getByLabelText('Save'));
    await settle();

    expect(unverifiedPosts().map((b) => b.scan_reference)).toEqual([
      { scan_record_id: REC, candidate_index: 0 },
    ]);
  });

  it('no reference when the scan response carried no record id', async () => {
    const utils = await scanAndReview([POND]);
    await fireEvent.press(utils.getByLabelText('Add Pond Notes yourself'));
    await settle();
    mockPost.mockResolvedValueOnce(saved('Pond Notes'));
    await fireEvent.press(utils.getByLabelText('Save'));
    await settle();

    expect(unverifiedPosts()).toEqual([{ title: 'Pond Notes', author: 'A. Author', isbn: null }]);
  });
});
