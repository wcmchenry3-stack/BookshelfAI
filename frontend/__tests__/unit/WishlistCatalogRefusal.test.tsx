import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react-native';

import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { BannerProvider } from '../../contexts/BannerContext';
import { useBanner } from '../../hooks/useBanner';
import { useScanJobs } from '../../hooks/useScanJobs';
import type { EnrichedBook } from '../../components/BookCandidatePicker';
import { catalogRefusal } from '../../lib/ownership';

// #612: POST /wishlist answers 422 book_not_in_catalog (no catalog match) or
// 422 catalog_token_invalid (stale result). Both are final: a specific
// message, no retry, no Sentry event.

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

describe('POST /wishlist 422 catalog refusals (#612)', () => {
  it('book_not_in_catalog: specific message, no retry, no Sentry', async () => {
    const hook = await scanAndAdd(['Dune'], () =>
      Promise.reject(httpError(422, 'book_not_in_catalog'))
    );
    expect(hook.result.current.banner.banner).toEqual(
      expect.objectContaining({
        message: "We couldn't find this book in our catalog. Search by title, or add it yourself.",
        type: 'error',
      })
    );
    expect(hook.result.current.scan.reviewingJob).toBeNull();
    expect(hook.result.current.scan.jobs).toHaveLength(0);
    expect(mockCaptureException).not.toHaveBeenCalled();
  });

  it('several books all not in the catalog', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma'], () =>
      Promise.reject(httpError(422, 'book_not_in_catalog'))
    );
    expect(hook.result.current.banner.banner?.message).toBe(
      "We couldn't find these 2 books in our catalog. Search by title, or add them yourself."
    );
    expect(hook.result.current.scan.reviewingJob).toBeNull();
  });

  it('catalog_token_invalid: specific message, no retry, no Sentry', async () => {
    const hook = await scanAndAdd(['Dune'], () =>
      Promise.reject(httpError(422, 'catalog_token_invalid'))
    );
    expect(hook.result.current.banner.banner).toEqual(
      expect.objectContaining({
        message: 'This result is out of date. Scan or search for the book again.',
        type: 'error',
      })
    );
    expect(hook.result.current.scan.reviewingJob).toBeNull();
    expect(hook.result.current.scan.jobs).toHaveLength(0);
    expect(mockCaptureException).not.toHaveBeenCalled();
  });

  it('several stale results', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma'], () =>
      Promise.reject(httpError(422, 'catalog_token_invalid'))
    );
    expect(hook.result.current.banner.banner?.message).toBe(
      'These 2 results are out of date. Scan or search for the books again.'
    );
  });

  it('mixed: one added, one not in catalog, one 500 keeps the 500 and the not-in-catalog book', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma', 'Ulysses'], (title) =>
      title === 'Dune'
        ? Promise.resolve({ data: {} })
        : title === 'Emma'
          ? Promise.reject(httpError(422, 'book_not_in_catalog'))
          : Promise.reject(httpError(500, 'boom'))
    );
    expect(hook.result.current.banner.banner).toEqual(
      expect.objectContaining({
        message:
          'Added 1, 2 not saved. 1 not in our catalog: try searching by title. Tap Add to retry the others.',
        type: 'error',
      })
    );
    // The not-in-catalog book stays in the open picker so its "Add it yourself"
    // button is still reachable (#616); the added book is gone.
    expect(hook.result.current.scan.reviewingJob?.results?.map((b) => b.title)).toEqual([
      'Emma',
      'Ulysses',
    ]);
    // Only the 500 is reported.
    expect(mockCaptureException).toHaveBeenCalledTimes(1);
  });

  it('mixed: added next to a stale result needs no retry hint and closes the review', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma'], (title) =>
      title === 'Dune'
        ? Promise.resolve({ data: {} })
        : Promise.reject(httpError(422, 'catalog_token_invalid'))
    );
    expect(hook.result.current.banner.banner?.message).toBe(
      'Added 1, 1 not saved. 1 out of date: scan or search again.'
    );
    expect(hook.result.current.scan.reviewingJob).toBeNull();
    expect(mockCaptureException).not.toHaveBeenCalled();
  });

  const own = () => Promise.reject(httpError(409, 'owned_same_edition'));
  const nic = () => Promise.reject(httpError(422, 'book_not_in_catalog'));
  const stale = () => Promise.reject(httpError(422, 'catalog_token_invalid'));
  const boom = () => Promise.reject(httpError(500, 'boom'));
  const ok = () => Promise.resolve({ data: {} });
  const pick = (m: Record<string, () => Promise<unknown>>) => (t: string) => m[t]();

  it('mixed: added, owned and not in catalog', async () => {
    const hook = await scanAndAdd(
      ['Dune', 'Emma', 'Ulysses'],
      pick({ Dune: ok, Emma: own, Ulysses: nic })
    );
    expect(hook.result.current.banner.banner?.message).toBe(
      'Added 1, 1 already in your library, 1 not saved. 1 not in our catalog: try searching by title.'
    );
    expect(hook.result.current.scan.reviewingJob).toBeNull();
    expect(mockCaptureException).not.toHaveBeenCalled();
  });

  it('mixed: owned and not in catalog, nothing added, has no "Added 0"', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma'], pick({ Dune: own, Emma: nic }));
    expect(hook.result.current.banner.banner?.message).toBe(
      '1 already in your library, 1 not saved. 1 not in our catalog: try searching by title.'
    );
  });

  it('both refusal kinds, nothing added', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma'], pick({ Dune: nic, Emma: stale }));
    expect(hook.result.current.banner.banner?.message).toBe(
      '2 not saved. 1 not in our catalog: try searching by title. 1 out of date: scan or search again.'
    );
    expect(hook.result.current.scan.reviewingJob).toBeNull();
  });

  it('refused plus a 500, nothing added', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma'], pick({ Dune: nic, Emma: boom }));
    expect(hook.result.current.banner.banner?.message).toBe(
      '2 not saved. 1 not in our catalog: try searching by title. Tap Add to retry the others.'
    );
    // Both stay in the open picker: the 500 to retry, the refused book for
    // "Add it yourself" (#616).
    expect(hook.result.current.scan.reviewingJob?.results?.map((b) => b.title)).toEqual([
      'Dune',
      'Emma',
    ]);
  });

  it('refused, owned and 500 together keep the 500 and the refused book', async () => {
    const hook = await scanAndAdd(
      ['Dune', 'Emma', 'Ulysses'],
      pick({ Dune: nic, Emma: own, Ulysses: boom })
    );
    expect(hook.result.current.banner.banner?.message).toBe(
      '1 already in your library, 2 not saved. 1 not in our catalog: try searching by title. Tap Add to retry the others.'
    );
    expect(hook.result.current.scan.reviewingJob?.results?.map((b) => b.title)).toEqual([
      'Dune',
      'Ulysses',
    ]);
    expect(mockCaptureException).toHaveBeenCalledTimes(1);
  });

  it('shows the whole banner, not two lines', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma'], pick({ Dune: nic, Emma: stale }));
    expect(hook.result.current.banner.banner?.numberOfLines).toBe(0);
  });

  it('a 422 with another detail is still a retryable failure', async () => {
    const hook = await scanAndAdd(['Dune'], () => Promise.reject(httpError(422, 'other')));
    expect(hook.result.current.banner.banner?.message).toBe(
      'Failed to add book to wishlist. Please try again.'
    );
    expect(hook.result.current.scan.reviewingJob).not.toBeNull();
    expect(mockCaptureException).toHaveBeenCalledTimes(1);
  });
});

describe('catalogRefusal', () => {
  it('recognises the two 422 codes only', () => {
    expect(catalogRefusal(httpError(422, 'book_not_in_catalog'))).toBe('book_not_in_catalog');
    expect(catalogRefusal(httpError(422, 'catalog_token_invalid'))).toBe('catalog_token_invalid');
    expect(catalogRefusal(httpError(409, 'book_not_in_catalog'))).toBeNull();
    expect(catalogRefusal(httpError(422, 'x'))).toBeNull();
    expect(catalogRefusal(new Error('network'))).toBeNull();
    expect(catalogRefusal(undefined)).toBeNull();
  });
});
