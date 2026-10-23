import React from 'react';
import { act, render, renderHook, waitFor } from '@testing-library/react-native';

import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { BannerProvider } from '../../contexts/BannerContext';
import { useBanner } from '../../hooks/useBanner';
import { useScanJobs } from '../../hooks/useScanJobs';
import type { EnrichedBook } from '../../components/BookCandidatePicker';
import WishlistScreen from '../../app/(tabs)/wishlist';
import MyBooksScreen from '../../app/(tabs)/my-books';

// #616: a 422 book_not_in_catalog offers "Add it yourself" on the banner,
// which opens the form pre-filled from each refused book; a saved entry is
// confirmed and shown in the library with the Unverified badge.

const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../../lib/api', () => ({
  api: {
    get: (...a: unknown[]) => mockGet(...a),
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
jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureException: jest.fn(), captureMessage: jest.fn() },
}));
let mockUuid = 0;
jest.mock('expo-crypto', () => ({ randomUUID: () => `mockUuid-${++mockUuid}` }));
jest.mock('expo-router', () => ({
  useFocusEffect: (cb: () => void) => {
    jest.requireActual<typeof import('react')>('react').useEffect(cb, []);
  },
}));
jest.mock('@expo/vector-icons', () => ({ MaterialIcons: () => null }));
jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: { background: '#fff', text: '#000', textSecondary: '#888', primary: '#007AFF' },
      typography: { fontSizeBase: 16, fontSizeSM: 14, fontSizeLG: 20, fontSizeXS: 12 },
    },
  }),
}));
jest.mock('../../components/LoadingSpinner', () => ({
  LoadingSpinner: () => null,
}));

const book = (title: string, matched = false): EnrichedBook => ({
  ...(matched ? { open_library_work_id: `OL-${title}` } : {}),
  title,
  author: 'A. Local',
  subjects: [],
  confidence: 0.6,
  already_in_library: false,
  ownership: 'not_owned',
  editions: [{ isbn_13: '9780306406157' }],
});

function httpError(status: number, detail: unknown) {
  return Object.assign(new Error(`Request failed with status code ${status}`), {
    response: { status, data: { detail } },
  });
}

beforeEach(() => {
  jest.resetAllMocks();
  mockUuid = 0;
});

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <BannerProvider>
      <ScanJobProvider>{children}</ScanJobProvider>
    </BannerProvider>
  );
}

async function scanAndAdd(books: EnrichedBook[], answer: (title: string) => Promise<unknown>) {
  const hook = await renderHook(() => ({ scan: useScanJobs(), banner: useBanner() }), {
    wrapper,
  });
  await waitFor(() => expect(hook.result.current.scan.jobs).toEqual([]));
  mockPost.mockResolvedValueOnce({
    data: { books, enhanced: false, enhanced_scans_remaining: null },
  });
  await act(async () => {
    await hook.result.current.scan.startScan('image', 'file:///a.jpg');
  });
  await act(() => {
    hook.result.current.scan.reviewJob(hook.result.current.scan.jobs[0].id);
  });
  mockPost.mockImplementation((_url: string, b: { title: string }) => answer(b.title));
  await act(async () => {
    await hook.result.current.scan.handleAddBooks(books);
  });
  return hook;
}

const nic = () => Promise.reject(httpError(422, 'book_not_in_catalog'));
const stale = () => Promise.reject(httpError(422, 'catalog_token_invalid'));
const ok = () => Promise.resolve({ data: {} });

describe('"Add it yourself" from a 422 book_not_in_catalog (#616)', () => {
  it('the banner offers it and opens the form pre-filled from the scan', async () => {
    const hook = await scanAndAdd([book('Pond Notes')], nic);
    const actions = hook.result.current.banner.banner?.actions ?? [];
    expect(actions.map((a) => a.label)).toEqual(['Add it yourself']);
    expect(hook.result.current.scan.unverifiedDrafts).toEqual([]);

    await act(() => actions[0].onPress());
    expect(hook.result.current.scan.unverifiedDrafts).toEqual([
      { title: 'Pond Notes', author: 'A. Local', isbn: '9780306406157' },
    ]);
  });

  it('one form per refused book; saving moves to the next and confirms', async () => {
    const hook = await scanAndAdd([book('Dune', true), book('Pond'), book('Lake')], (t) =>
      t === 'Dune' ? ok() : nic()
    );
    const [action] = hook.result.current.banner.banner?.actions ?? [];
    await act(() => action.onPress());
    expect(hook.result.current.scan.unverifiedDrafts.map((d) => d.title)).toEqual(['Pond', 'Lake']);

    await act(() => hook.result.current.scan.unverifiedSaved('Pond'));
    expect(hook.result.current.scan.unverifiedDrafts.map((d) => d.title)).toEqual(['Lake']);
    expect(hook.result.current.banner.banner).toEqual(
      expect.objectContaining({
        message: 'Added Pond to your wishlist as unverified.',
        type: 'success',
      })
    );

    await act(() => hook.result.current.scan.dismissUnverified());
    expect(hook.result.current.scan.unverifiedDrafts).toEqual([]);
  });

  it('is not offered for a stale result only', async () => {
    const hook = await scanAndAdd([book('Dune', true)], stale);
    expect(hook.result.current.banner.banner?.actions).toBeUndefined();
  });

  it('addYourself from the picker opens the form for that book', async () => {
    const hook = await renderHook(() => useScanJobs(), { wrapper });
    await act(() => hook.result.current.addYourself([book('Pond')]));
    expect(hook.result.current.unverifiedDrafts).toEqual([
      { title: 'Pond', author: 'A. Local', isbn: '9780306406157' },
    ]);
  });
});

const UNVERIFIED_ENTRY = {
  id: 'ub-u',
  status: 'wishlisted',
  rating: null,
  notes: null,
  wishlisted_at: null,
  purchased_at: null,
  started_at: null,
  finished_at: null,
  unverified: true,
  unverified_isbn_13: '9780306406157',
  book: {
    id: 'c-1',
    title: 'Pond Notes',
    author: 'A. Local',
    cover_url: null,
    description: null,
  },
  edition: null,
};
const CATALOG_ENTRY = {
  ...UNVERIFIED_ENTRY,
  id: 'ub-c',
  unverified: false,
  unverified_isbn_13: null,
  book: { ...UNVERIFIED_ENTRY.book, id: 'b-1', title: 'Dune' },
};

describe('Unverified badge in the library (#616)', () => {
  it.each([
    ['wishlist', WishlistScreen],
    ['my books', MyBooksScreen],
  ])('%s shows it on unverified entries only', async (_name, Screen) => {
    mockGet.mockResolvedValue({ data: [UNVERIFIED_ENTRY, CATALOG_ENTRY] });
    const { findByText, getAllByTestId } = await render(<Screen />);
    expect(await findByText('Pond Notes')).toBeTruthy();
    expect(await findByText('Dune')).toBeTruthy();
    const badges = getAllByTestId('unverified-badge');
    expect(badges).toHaveLength(1);
    expect(badges[0].props.accessibilityLabel).toBe(
      'Unverified: added by you, not found in the catalog'
    );
  });
});
