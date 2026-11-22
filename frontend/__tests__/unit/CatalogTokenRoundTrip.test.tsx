import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react-native';

import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { BannerProvider } from '../../contexts/BannerContext';
import { useBanner } from '../../hooks/useBanner';
import { useScanJobs } from '../../hooks/useScanJobs';
import type { EnrichedBook } from '../../components/BookCandidatePicker';

// #602: every book from POST /scan and GET /books/search carries a
// server-signed `catalog_token`. The app must post it back unchanged, with
// every catalog field as received, to POST /wishlist; a 422
// `catalog_token_invalid` or `book_not_in_catalog` shows the existing
// "couldn't save" message.

const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../../hooks/useAuth', () => ({ useAuth: () => ({ isAuthenticated: true }) }));
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
let mockUuid = 0;
jest.mock('expo-crypto', () => ({ randomUUID: () => `mockUuid-${++mockUuid}` }));

const signed = (title: string, token: string): EnrichedBook => ({
  open_library_work_id: `OL-${title}`,
  title,
  author: 'Frank Herbert',
  description: 'Spice.',
  cover_url: 'https://books.google.com/c.jpg',
  subjects: ['Science fiction'],
  confidence: 0.9,
  already_in_library: false,
  in_library: false,
  editions: [{ isbn_13: '9780441013593', publisher: 'Ace', format: 'unknown' }],
  catalog_token: token,
});
const base = { enhanced: false, enhanced_scans_remaining: null };

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

async function render() {
  const hook = await renderHook(() => ({ scan: useScanJobs(), banner: useBanner() }), {
    wrapper,
  });
  await waitFor(() => expect(hook.result.current.scan.jobs).toEqual([]));
  return hook;
}

type Hook = Awaited<ReturnType<typeof render>>;

async function addReviewedResults(hook: Hook) {
  await act(() => {
    hook.result.current.scan.reviewJob(hook.result.current.scan.jobs[0].id);
  });
  const results = hook.result.current.scan.reviewingJob?.results ?? [];
  await act(async () => {
    await hook.result.current.scan.handleAddBooks(results);
  });
  return mockPost.mock.calls.filter(([url]) => url === '/wishlist').map(([, body]) => body);
}

describe('catalog_token round trip (#602)', () => {
  it('posts each scanned book back to /wishlist with its token and fields unchanged', async () => {
    const hook = await render();
    const dune = signed('Dune', 'payload-1.mac-1');
    const emma = signed('Emma', 'payload-2.mac-2');
    mockPost.mockResolvedValueOnce({ data: { ...base, books: [dune, emma] } });
    await act(async () => {
      await hook.result.current.scan.startScan('image', 'file:///a.jpg');
    });
    mockPost.mockResolvedValue({ data: {} });

    const posted = await addReviewedResults(hook);

    expect(posted).toEqual([dune, emma]);
    expect(posted.map((b: EnrichedBook) => b.catalog_token)).toEqual([
      'payload-1.mac-1',
      'payload-2.mac-2',
    ]);
  });

  it('keeps the token of a repeat served from a scan record', async () => {
    const hook = await render();
    const dune = signed('Dune', 'repeat-payload.mac');
    mockPost.mockResolvedValueOnce({ data: { ...base, repeat: true, books: [dune] } });
    await act(async () => {
      await hook.result.current.scan.startScan('image', 'file:///a.jpg');
    });
    mockPost.mockResolvedValue({ data: {} });

    const posted = await addReviewedResults(hook);

    expect(posted).toEqual([dune]);
  });

  it('keeps the token of a /books/search result', async () => {
    const hook = await render();
    const dune = signed('Dune', 'search-payload.mac');
    mockGet.mockResolvedValueOnce({ data: [dune] });
    await act(async () => {
      await hook.result.current.scan.startScan('text', undefined, 'Dune');
    });
    mockPost.mockResolvedValue({ data: {} });

    const posted = await addReviewedResults(hook);

    expect(mockGet).toHaveBeenCalledWith('/books/search', {
      params: { q: 'Dune' },
      signal: expect.any(AbortSignal),
      timeout: 45_000,
    });
    expect(posted).toEqual([dune]);
  });

  it.each([
    ['catalog_token_invalid', 'This result is out of date. Scan or search for the book again.'],
    [
      'book_not_in_catalog',
      "We couldn't find this book in our catalog. Search by title, or add it yourself.",
    ],
  ])('shows the specific message on 422 %s (#612)', async (detail, message) => {
    const hook = await render();
    mockPost.mockResolvedValueOnce({ data: { ...base, books: [signed('Dune', 'old.mac')] } });
    await act(async () => {
      await hook.result.current.scan.startScan('image', 'file:///a.jpg');
    });
    mockPost.mockRejectedValue(
      Object.assign(new Error('Request failed with status code 422'), {
        response: { status: 422, data: { detail } },
      })
    );

    await addReviewedResults(hook);

    expect(hook.result.current.banner.banner?.message).toBe(message);
    // Retrying cannot help, so nothing stays under review.
    expect(hook.result.current.scan.reviewingJob).toBeNull();
  });
});
