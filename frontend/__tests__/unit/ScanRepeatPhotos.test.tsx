import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react-native';

import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { BannerProvider } from '../../contexts/BannerContext';
import { useBanner } from '../../hooks/useBanner';
import { useScanJobs } from '../../hooks/useScanJobs';
import type { EnrichedBook } from '../../components/BookCandidatePicker';
import { repeatOutcome } from '../../lib/scanJob';
import { LOCALES } from '../../src/i18n/locales';

// #588: a photo the user already scanned is explained, not shown as a new scan.
// Driven by the `repeat` / `in_library` fields the server sends (#585).

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
let mockUuid = 0;
jest.mock('expo-crypto', () => ({ randomUUID: () => `mockUuid-${++mockUuid}` }));

const book = (title: string, inLibrary: boolean) =>
  ({
    title,
    author: 'A',
    already_in_library: inLibrary,
    in_library: inLibrary,
  }) as unknown as EnrichedBook;
const base = { enhanced: false, enhanced_scans_remaining: null };

beforeEach(() => {
  jest.resetAllMocks();
  mockUuid = 0;
});

describe('repeatOutcome', () => {
  it('is none for a normal scan', () => {
    expect(repeatOutcome({ books: [book('a', true)] } as never)).toBe('none');
    expect(repeatOutcome({ repeat: false, books: [] })).toBe('none');
    expect(repeatOutcome(undefined)).toBe('none');
  });
  it('is no_match for a repeat with no books', () => {
    expect(repeatOutcome({ repeat: true, books: [] })).toBe('no_match');
  });
  it('is all_in_library when every book is in the library', () => {
    expect(repeatOutcome({ repeat: true, books: [book('a', true), book('b', true)] })).toBe(
      'all_in_library'
    );
  });
  it('is not_in_library when any book is missing', () => {
    expect(repeatOutcome({ repeat: true, books: [book('a', true), book('b', false)] })).toBe(
      'not_in_library'
    );
  });
  it('falls back to already_in_library when in_library is absent', () => {
    const b = { title: 'a', author: 'A', already_in_library: true } as never;
    expect(repeatOutcome({ repeat: true, books: [b] })).toBe('all_in_library');
    const c = { title: 'a', author: 'A', already_in_library: false } as never;
    expect(repeatOutcome({ repeat: true, books: [c] })).toBe('not_in_library');
  });
});

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <BannerProvider>
      <ScanJobProvider>{children}</ScanJobProvider>
    </BannerProvider>
  );
}

async function scan(data: Record<string, unknown>) {
  const hook = await renderHook(() => ({ scan: useScanJobs(), banner: useBanner() }), {
    wrapper,
  });
  await waitFor(() => expect(hook.result.current.scan.jobs).toEqual([]));
  mockPost.mockResolvedValueOnce({ data });
  const before = hook.result.current.scan.usageRefreshKey;
  await act(async () => {
    await hook.result.current.scan.startScan('image', 'file:///a.jpg');
  });
  return { hook, before };
}

describe('repeat photo messages', () => {
  it('one book already in the library: "You already have this book."', async () => {
    const { hook } = await scan({ ...base, repeat: true, books: [book('Dune', true)] });
    expect(hook.result.current.banner.banner?.message).toBe('You already have this book.');
    expect(hook.result.current.scan.jobs).toEqual([]);
    expect(hook.result.current.scan.reviewingJob).toBeNull();
  });

  it('all in the library: the discarded photo is deleted from disk', async () => {
    const { deleteScanImage } = jest.requireMock('../../lib/scanJobStorage');
    await scan({ ...base, repeat: true, books: [book('Dune', true)] });
    expect(deleteScanImage).toHaveBeenCalledWith('file:///a.jpg');
  });

  it('several books all in the library: plural copy', async () => {
    const { hook } = await scan({
      ...base,
      repeat: true,
      books: [book('Dune', true), book('Emma', true)],
    });
    expect(hook.result.current.banner.banner?.message).toBe('You already have these books.');
  });

  it('some books not in the library: offers Re-add', async () => {
    const { hook } = await scan({
      ...base,
      repeat: true,
      books: [book('Dune', true), book('Emma', false)],
    });
    const banner = hook.result.current.banner.banner;
    expect(banner?.message).toBe(
      "You scanned this before and it's not in your library. Re-add it?"
    );
    expect(banner?.actions?.[0].label).toBe('Re-add');
    expect(hook.result.current.scan.jobs[0].status).toBe('complete');
  });

  it('a repeat of a no-book photo: try a clearer photo', async () => {
    const { hook } = await scan({ ...base, repeat: true, books: [], no_match: null });
    expect(hook.result.current.banner.banner?.message).toBe(
      "We couldn't find a book in this photo before. Try a clearer photo or a different angle."
    );
    expect(hook.result.current.scan.jobs[0].error).toBe('no_results');
  });

  it('a normal scan still shows "books found"', async () => {
    const { hook } = await scan({ ...base, books: [book('Dune', false)] });
    expect(hook.result.current.banner.banner?.message).toBe('Found 1 book');
  });
});

describe('enhanced re-scan served as a repeat', () => {
  it('a photo already enhanced, its books all in the library: "You already have…"', async () => {
    const { hook } = await scan({ ...base, books: [book('Dune', false)] });
    const jobId = hook.result.current.scan.jobs[0].id;
    mockPost.mockResolvedValueOnce({
      data: { ...base, enhanced: true, repeat: true, books: [book('Dune', true)] },
    });
    await act(async () => {
      await hook.result.current.scan.requestEnhancedScan(jobId);
    });
    expect(hook.result.current.banner.banner?.message).toBe('You already have this book.');
    expect(hook.result.current.scan.jobs).toEqual([]);
  });

  it('a photo already enhanced, a book not in the library: offers Re-add', async () => {
    const { hook } = await scan({ ...base, books: [book('Dune', false)] });
    const jobId = hook.result.current.scan.jobs[0].id;
    mockPost.mockResolvedValueOnce({
      data: { ...base, enhanced: true, repeat: true, books: [book('Dune', false)] },
    });
    await act(async () => {
      await hook.result.current.scan.requestEnhancedScan(jobId);
    });
    expect(hook.result.current.banner.banner?.actions?.[0].label).toBe('Re-add');
  });
});

describe('usage counters', () => {
  it.each([
    ['all in library', [book('Dune', true)]],
    ['not in library', [book('Dune', false)]],
    ['no book', []],
  ])('do not refetch on a repeat (%s)', async (_n, books) => {
    const { hook, before } = await scan({ ...base, repeat: true, books });
    expect(hook.result.current.scan.usageRefreshKey).toBe(before);
  });

  it('refetch after a normal scan', async () => {
    const { hook, before } = await scan({ ...base, books: [book('Dune', false)] });
    expect(hook.result.current.scan.usageRefreshKey).toBe(before + 1);
  });
});

describe('re-add flow', () => {
  it('opens the saved results and posts only /wishlist, never /scan again', async () => {
    const { hook } = await scan({
      ...base,
      repeat: true,
      books: [book('Dune', true), book('Emma', false)],
    });
    expect(mockPost.mock.calls.filter(([url]) => url === '/scan')).toHaveLength(1);

    expect(hook.result.current.banner.banner?.actions?.[0].label).toBe('Re-add');
    await act(async () => {
      hook.result.current.banner.banner?.actions?.[0].onPress();
    });
    expect(hook.result.current.scan.reviewingJob?.results?.map((b) => b.title)).toEqual([
      'Dune',
      'Emma',
    ]);

    mockPost.mockResolvedValue({ data: {} });
    await act(async () => {
      await hook.result.current.scan.handleAddBooks([book('Emma', false)]);
    });
    const urls = mockPost.mock.calls.map(([url]) => url);
    expect(urls).toEqual(['/scan', '/wishlist']);
    expect(hook.result.current.scan.reviewingJob).toBeNull();
  });
});

describe('every locale has the repeat copy', () => {
  const KEYS = [
    'repeatAlreadyHaveOne',
    'repeatAlreadyHaveMany',
    'repeatNotInLibrary',
    'repeatReAdd',
    'repeatNoMatch',
  ];
  it.each(LOCALES.map((l) => l.code))('%s', (code) => {
    const scan = require(`../../src/i18n/locales/${code}/scan.json`);
    for (const key of KEYS) {
      expect([key, typeof scan[key] === 'string' && scan[key].length > 0]).toEqual([key, true]);
      expect(scan[key]).not.toContain('NEEDS_TRANSLATION');
    }
  });

  // The number of books is never shown, so the copy is chosen on one vs more
  // than one, not with count plurals: Russian puts 21, 31… in the "one"
  // category, which would say "this book" for 21 books.
  it.each(LOCALES.map((l) => l.code))('%s has no count-plural repeat keys', (code) => {
    const scan = require(`../../src/i18n/locales/${code}/scan.json`);
    expect(Object.keys(scan).filter((k) => k.startsWith('repeatAlreadyHave_'))).toEqual([]);
  });

  it('_meta describes every new key with a character limit', () => {
    const meta = require('../../src/i18n/locales/_meta/scan.meta.json');
    for (const key of KEYS) {
      expect([key, typeof meta[key]?.description]).toEqual([key, 'string']);
      expect([key, typeof meta[key]?.characterLimit]).toEqual([key, 'number']);
    }
  });
});
