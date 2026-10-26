import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react-native';

import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { BannerProvider } from '../../contexts/BannerContext';
import { useBanner } from '../../hooks/useBanner';
import { useScanJobs } from '../../hooks/useScanJobs';
import type { EnrichedBook } from '../../components/BookCandidatePicker';
import { isAlreadyOwnedError, isOwned } from '../../lib/ownership';

// #587: POST /wishlist answers 409 with an ownership code (owned_same_edition,
// owned_other_edition, owned_edition_unknown) for a book the user already
// has. The app treats it as "already in your library": not a failure, no
// retry, no Sentry event.

const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../../lib/api', () => ({
  api: {
    get: (...a: unknown[]) => mockGet(...a),
    post: (...a: unknown[]) => mockPost(...a),
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
const mockCaptureException = jest.fn();
jest.mock('../../lib/sentry', () => ({
  Sentry: {
    addBreadcrumb: jest.fn(),
    captureException: (...a: unknown[]) => mockCaptureException(...a),
    captureMessage: jest.fn(),
  },
}));
let mockUuid = 0;
jest.mock('expo-crypto', () => ({ randomUUID: () => `mockUuid-${++mockUuid}` }));

const book = (title: string): EnrichedBook => ({
  open_library_work_id: `OL-${title}`,
  title,
  author: 'Frank Herbert',
  subjects: [],
  confidence: 0.9,
  already_in_library: false,
  in_library: false,
  ownership: 'not_owned',
  editions: [{ isbn_13: '9780441013593' }],
  catalog_token: 'p.mac',
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

async function scanAndAdd(titles: string[], answer: (title: string) => Promise<unknown>) {
  const hook = await renderHook(() => ({ scan: useScanJobs(), banner: useBanner() }), {
    wrapper,
  });
  await waitFor(() => expect(hook.result.current.scan.jobs).toEqual([]));
  mockPost.mockResolvedValueOnce({
    data: { books: titles.map(book), enhanced: false, enhanced_scans_remaining: null },
  });
  await act(async () => {
    await hook.result.current.scan.startScan('image', 'file:///a.jpg');
  });
  await act(() => {
    hook.result.current.scan.reviewJob(hook.result.current.scan.jobs[0].id);
  });
  mockPost.mockImplementation((_url: string, b: { title: string }) => answer(b.title));
  await act(async () => {
    await hook.result.current.scan.handleAddBooks(titles.map(book));
  });
  return hook;
}

const CODES = ['owned_same_edition', 'owned_other_edition', 'owned_edition_unknown'];

describe('POST /wishlist 409 = already in your library (#587)', () => {
  it.each(CODES)('one book answered 409 %s', async (code) => {
    const hook = await scanAndAdd(['Dune'], () => Promise.reject(httpError(409, code)));

    expect(hook.result.current.banner.banner).toEqual(
      expect.objectContaining({
        message: '"Dune" is already in your library.',
        type: 'info',
      })
    );
    // Done with: the review closes, nothing is kept to retry.
    expect(hook.result.current.scan.reviewingJob).toBeNull();
    expect(hook.result.current.scan.jobs).toHaveLength(0);
    expect(mockCaptureException).not.toHaveBeenCalled();
  });

  it('several books all already owned', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma'], () =>
      Promise.reject(httpError(409, 'owned_other_edition'))
    );
    expect(hook.result.current.banner.banner?.message).toBe('2 books are already in your library.');
    expect(hook.result.current.scan.reviewingJob).toBeNull();
  });

  it('some added, some already owned', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma', 'Ulysses'], (title) =>
      title === 'Emma'
        ? Promise.reject(httpError(409, 'owned_same_edition'))
        : Promise.resolve({ data: {} })
    );
    expect(hook.result.current.banner.banner).toEqual(
      expect.objectContaining({
        message: 'Added 2. 1 already in your library.',
        type: 'success',
      })
    );
    expect(hook.result.current.scan.reviewingJob).toBeNull();
    expect(mockCaptureException).not.toHaveBeenCalled();
  });

  it('a real failure next to an owned book keeps only the failed one to retry', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma'], (title) =>
      title === 'Emma'
        ? Promise.reject(httpError(500, 'boom'))
        : Promise.reject(httpError(409, 'owned_edition_unknown'))
    );
    expect(hook.result.current.scan.reviewingJob?.results?.map((b) => b.title)).toEqual(['Emma']);
    expect(mockCaptureException).toHaveBeenCalledTimes(1);
    // Nothing was added: the owned book is not counted as added.
    expect(hook.result.current.banner.banner).toEqual(
      expect.objectContaining({
        message: '1 already in your library, 1 not saved. Tap Add to retry.',
        type: 'error',
      })
    );
  });

  it('added, owned and failed together are each counted', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma', 'Ulysses'], (title) =>
      title === 'Dune'
        ? Promise.resolve({ data: {} })
        : title === 'Emma'
          ? Promise.reject(httpError(409, 'owned_other_edition'))
          : Promise.reject(httpError(500, 'boom'))
    );
    expect(hook.result.current.banner.banner?.message).toBe(
      'Added 1, 1 already in your library, 1 not saved. Tap Add to retry.'
    );
    expect(hook.result.current.scan.reviewingJob?.results?.map((b) => b.title)).toEqual([
      'Ulysses',
    ]);
  });

  it('a 409 without an ownership code is still a failure', async () => {
    const hook = await scanAndAdd(['Dune'], () => Promise.reject(httpError(409, 'other')));
    expect(hook.result.current.banner.banner?.message).toBe(
      'Failed to add book to wishlist. Please try again.'
    );
    expect(hook.result.current.scan.reviewingJob).not.toBeNull();
  });
});

describe('ownership helpers', () => {
  it.each(CODES)('isAlreadyOwnedError(409 %s)', (code) => {
    expect(isAlreadyOwnedError(httpError(409, code))).toBe(true);
  });

  it.each([
    [httpError(409, 'not_owned')],
    [httpError(422, 'owned_same_edition')],
    [new Error('network')],
    [undefined],
  ])('is not an ownership refusal: %p', (reason) => {
    expect(isAlreadyOwnedError(reason)).toBe(false);
  });

  it('isOwned uses ownership, else the work-level flags of older results', () => {
    expect(isOwned({ ownership: 'owned_other_edition' })).toBe(true);
    expect(isOwned({ ownership: 'not_owned', already_in_library: true })).toBe(false);
    expect(isOwned({ in_library: true })).toBe(true);
    expect(isOwned({ already_in_library: true })).toBe(true);
    expect(isOwned({ already_in_library: false })).toBe(false);
  });
});
