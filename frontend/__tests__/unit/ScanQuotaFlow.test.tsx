import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react-native';

import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { BannerProvider } from '../../contexts/BannerContext';
import { useBanner } from '../../hooks/useBanner';
import { useScanJobs } from '../../hooks/useScanJobs';

const mockPost = jest.fn();
jest.mock('../../lib/api', () => ({
  api: { post: (...a: unknown[]) => mockPost(...a), get: jest.fn() },
}));

const mockSaveJobs = jest.fn();
const mockLoadJobs = jest.fn();
const mockNetFetch = jest.fn();
let mockNetListener: ((s: { isConnected: boolean }) => void) | null = null;
jest.mock('../../lib/scanJobStorage', () => ({
  loadJobs: () => mockLoadJobs(),
  saveJobs: (...a: unknown[]) => mockSaveJobs(...a),
  clearJobs: jest.fn(),
  sweepOrphanedScanFiles: () => Promise.resolve(0),
  deleteScanImage: jest.fn(),
}));

jest.mock('@react-native-community/netinfo', () => ({
  __esModule: true,
  default: {
    fetch: () => mockNetFetch(),
    addEventListener: (cb: (s: { isConnected: boolean }) => void) => {
      mockNetListener = cb;
      return () => {};
    },
  },
}));

let mockUuid = 0;
jest.mock('expo-crypto', () => ({ randomUUID: () => `mockUuid-${++mockUuid}` }));

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <BannerProvider>
      <ScanJobProvider>{children}</ScanJobProvider>
    </BannerProvider>
  );
}

function refusal(status: number, detail: string, extra = {}, headers = {}) {
  return { response: { status, data: { detail, ...extra }, headers } };
}

const OK = {
  data: {
    books: [{ title: 'Dune', author: 'Herbert' }],
    enhanced: false,
    enhanced_scans_remaining: 5,
  },
};

async function setup() {
  const hook = await renderHook(() => useScanJobs(), { wrapper });
  await waitFor(() => expect(mockSaveJobs).toHaveBeenCalled());
  return hook;
}

beforeEach(() => {
  jest.resetAllMocks();
  mockUuid = 0;
  mockSaveJobs.mockResolvedValue(undefined);
  mockLoadJobs.mockResolvedValue([]);
  mockNetFetch.mockResolvedValue({ isConnected: true });
});

afterEach(() => {
  jest.useRealTimers();
});

describe('scan refusals', () => {
  it('402 scan_limit_reached from an older server is a generic failure, not a notice', async () => {
    // #582: there is no monthly limit; the generic failure banner (retry /
    // save for later) is the fallback for a 402 an older server still sends.
    const hook = await renderHook(() => ({ scan: useScanJobs(), banner: useBanner() }), {
      wrapper,
    });
    await waitFor(() => expect(mockSaveJobs).toHaveBeenCalled());
    const { result } = hook;
    mockPost.mockRejectedValueOnce(
      refusal(402, 'scan_limit_reached', { limit: 5, used: 5, period_end: '2026-11-01' })
    );
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///a.jpg');
    });
    expect(result.current.scan.scanNotice).toBeNull();
    expect(result.current.scan.jobs[0].status).toBe('failed');
    expect(result.current.banner.banner?.message).toBe('Scan failed');
    expect(result.current.banner.banner?.message).not.toMatch(/month/i);
    expect(mockPost).toHaveBeenCalledTimes(1);
  });

  it('429 daily_scan_limit sets the daily notice', async () => {
    const { result } = await setup();
    mockPost.mockRejectedValueOnce(refusal(429, 'daily_scan_limit', {}, { 'retry-after': '3600' }));
    await act(async () => {
      await result.current.startScan('image', 'file:///a.jpg');
    });
    expect(result.current.scanNotice?.kind).toBe('daily_limit');
    const n = result.current.scanNotice as { resetsAt: number };
    expect(Math.abs(n.resetsAt - (Date.now() + 3_600_000))).toBeLessThan(5000);
  });

  it.each(['scan_disabled', 'scan_capacity', 'ai_unavailable', 'scan_unavailable'])(
    '503 %s sets the paused notice',
    async (code) => {
      const { result } = await setup();
      mockPost.mockRejectedValueOnce(refusal(503, code));
      await act(async () => {
        await result.current.startScan('image', 'file:///a.jpg');
      });
      expect(result.current.scanNotice).toEqual({
        kind: 'paused',
      });
    }
  );

  // #533 replaced 402 no_enhanced_credits with these enhanced-only refusals.
  const ENHANCED_REFUSALS: [string, ReturnType<typeof refusal>, string][] = [
    ['403 enhanced_requires_premium', refusal(403, 'enhanced_requires_premium'), 'Premium'],
    [
      '429 enhanced_throttled',
      refusal(429, 'enhanced_throttled', {}, { 'retry-after': '3600' }),
      'found no books',
    ],
  ];

  it.each(ENHANCED_REFUSALS)(
    '%s falls back without a notice and hides the offer',
    async (_name, err, text) => {
      const hook = await renderHook(() => ({ scan: useScanJobs(), banner: useBanner() }), {
        wrapper,
      });
      await waitFor(() => expect(mockSaveJobs).toHaveBeenCalled());
      const { result } = hook;
      mockPost.mockResolvedValueOnce({ data: { ...OK.data, books: [] } });
      await act(async () => {
        await result.current.scan.startScan('image', 'file:///a.jpg');
      });
      mockPost.mockRejectedValueOnce(err);
      await act(async () => {
        await result.current.scan.requestEnhancedScan(result.current.scan.jobs[0].id);
      });
      expect(result.current.scan.enhancedScansRemaining).toBe(0);
      expect(result.current.scan.scanNotice).toBeNull();
      expect(result.current.scan.jobs[0]).toEqual(
        expect.objectContaining({ status: 'failed', enhanced: false, error: 'no_results' })
      );
      expect(result.current.banner.banner?.message).toContain(text);
      // Only one request: a refusal is never retried.
      expect(mockPost).toHaveBeenCalledTimes(2);
    }
  );

  it.each(ENHANCED_REFUSALS)(
    '%s on a re-scan restores the standard scan books',
    async (_name, err) => {
      const { result } = await setup();
      mockPost.mockResolvedValueOnce(OK);
      await act(async () => {
        await result.current.startScan('image', 'file:///a.jpg');
      });
      mockPost.mockRejectedValueOnce(err);
      await act(async () => {
        await result.current.requestEnhancedScan(result.current.jobs[0].id);
      });
      expect(result.current.jobs[0]).toEqual(
        expect.objectContaining({ status: 'complete', enhanced: false })
      );
      expect(result.current.reviewingJob?.results?.map((b) => b.title)).toEqual(['Dune']);
    }
  );

  it('offers the enhanced re-scan only when the server reports scans left', async () => {
    const hook = await renderHook(() => ({ scan: useScanJobs(), banner: useBanner() }), {
      wrapper,
    });
    await waitFor(() => expect(mockSaveJobs).toHaveBeenCalled());
    const { result } = hook;
    // A Free user: the server reports null (enhanced is Premium-only).
    mockPost.mockResolvedValueOnce({
      data: { books: [], enhanced: false, enhanced_scans_remaining: null },
    });
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///a.jpg');
    });
    expect(result.current.scan.enhancedScansRemaining).toBeNull();
    expect(result.current.banner.banner?.actions).toBeUndefined();

    // Premium with scans left: offered.
    mockPost.mockResolvedValueOnce({
      data: { books: [], enhanced: false, enhanced_scans_remaining: 3 },
    });
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///b.jpg');
    });
    expect(result.current.scan.enhancedScansRemaining).toBe(3);
    expect(result.current.banner.banner?.actions?.[0]?.label).toBe('Try enhanced scan');

    // Premium with none left: not offered.
    mockPost.mockResolvedValueOnce({
      data: { books: [], enhanced: false, enhanced_scans_remaining: 0 },
    });
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///c.jpg');
    });
    expect(result.current.banner.banner?.actions).toBeUndefined();
  });

  it('a successful scan clears the notice and bumps the usage refresh key', async () => {
    const { result } = await setup();
    mockPost.mockRejectedValueOnce(refusal(503, 'scan_disabled'));
    await act(async () => {
      await result.current.startScan('image', 'file:///a.jpg');
    });
    const before = result.current.usageRefreshKey;
    mockPost.mockResolvedValueOnce(OK);
    mockPost.mockResolvedValueOnce(OK); // the automatic resend of the refused photo
    await act(async () => {
      await result.current.startScan('image', 'file:///b.jpg');
    });
    expect(result.current.scanNotice).toBeNull();
    expect(result.current.usageRefreshKey).toBeGreaterThan(before);
  });

  it('dismissScanNotice clears the notice', async () => {
    const { result } = await setup();
    mockPost.mockRejectedValueOnce(refusal(503, 'scan_disabled'));
    await act(async () => {
      await result.current.startScan('image', 'file:///a.jpg');
    });
    await act(async () => result.current.dismissScanNotice());
    expect(result.current.scanNotice).toBeNull();
  });
});

describe('429 scan_in_progress', () => {
  const busy = () => refusal(429, 'scan_in_progress', {}, { 'retry-after': '2' });

  it('retries silently after Retry-After and succeeds', async () => {
    const { result } = await setup();
    jest.useFakeTimers();
    mockPost.mockRejectedValueOnce(busy()).mockResolvedValueOnce(OK);
    let started: Promise<void> | void;
    await act(async () => {
      started = result.current.startScan('image', 'file:///a.jpg');
      await jest.advanceTimersByTimeAsync(1999);
    });
    // Still waiting out Retry-After: no second attempt, no notice.
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(result.current.scanNotice).toBeNull();
    await act(async () => {
      await jest.advanceTimersByTimeAsync(1);
      await started;
    });
    expect(mockPost).toHaveBeenCalledTimes(2);
    expect(result.current.scanNotice).toBeNull();
    expect(result.current.jobs[0].status).toBe('complete');
  });

  it('shows "still processing" after 3 silent retries', async () => {
    const { result } = await setup();
    jest.useFakeTimers();
    mockPost.mockRejectedValue(busy());
    let started: Promise<void> | void;
    await act(async () => {
      started = result.current.startScan('image', 'file:///a.jpg');
      await jest.advanceTimersByTimeAsync(5999);
    });
    // 1 initial attempt + 2 retries so far; the third retry is not due yet.
    expect(mockPost).toHaveBeenCalledTimes(3);
    expect(result.current.scanNotice).toBeNull();
    await act(async () => {
      await jest.advanceTimersByTimeAsync(1);
      await started;
    });
    // 1 initial attempt + 3 retries, then give up.
    expect(mockPost).toHaveBeenCalledTimes(4);
    expect(result.current.scanNotice).toEqual({
      kind: 'still_processing',
    });
    expect(result.current.jobs[0].status).toBe('queued');
  });
});

describe('refusal follow-ups', () => {
  it('bumps usage after a generic 500 from /scan, not after a network error', async () => {
    const { result } = await setup();
    const k0 = result.current.usageRefreshKey;
    mockPost.mockRejectedValueOnce({ response: { status: 500, data: {} } });
    await act(async () => {
      await result.current.startScan('image', 'file:///a.jpg');
    });
    const k1 = result.current.usageRefreshKey;
    expect(k1).toBeGreaterThan(k0);
    mockPost.mockRejectedValueOnce({ message: 'Network Error', response: undefined });
    await act(async () => {
      await result.current.startScan('image', 'file:///b.jpg');
    });
    expect(result.current.usageRefreshKey).toBe(k1);
  });

  it('does not bump usage for paused refusals', async () => {
    const { result } = await setup();
    const k0 = result.current.usageRefreshKey;
    mockPost.mockRejectedValueOnce(refusal(503, 'scan_disabled'));
    await act(async () => {
      await result.current.startScan('image', 'file:///a.jpg');
    });
    expect(result.current.usageRefreshKey).toBe(k0);
  });

  it('a refusal leaves the job queued, counted as pending, with no retry budget used', async () => {
    const { result } = await setup();
    mockPost.mockRejectedValueOnce(refusal(503, 'scan_disabled'));
    await act(async () => {
      await result.current.startScan('image', 'file:///a.jpg');
    });
    expect(result.current.jobs[0]).toEqual(
      expect.objectContaining({ status: 'queued', error: 'paused', retryCount: 0 })
    );
    expect(result.current.pendingCount).toBe(1);
  });

  it('two refusals, then Try again sends both queued jobs', async () => {
    const { result } = await setup();
    mockPost.mockRejectedValue(refusal(503, 'scan_disabled'));
    await act(async () => {
      await result.current.startScan('image', 'file:///a.jpg');
      await result.current.startScan('image', 'file:///b.jpg');
    });
    expect(mockPost).toHaveBeenCalledTimes(2);
    expect(result.current.jobs.map((j) => j.status)).toEqual(['queued', 'queued']);
    jest.useFakeTimers();
    mockPost.mockReset();
    mockPost.mockResolvedValue(OK);
    await act(async () => {
      result.current.resumeQueue();
      await jest.advanceTimersByTimeAsync(10000);
    });
    expect(result.current.scanNotice).toBeNull();
    expect(mockPost).toHaveBeenCalledTimes(2);
    expect(result.current.jobs.map((j) => j.status)).toEqual(['complete', 'complete']);
  });

  it('a refused enhanced re-scan restores earlier results silently and shows the notice', async () => {
    const { result } = await setup();
    mockPost.mockResolvedValueOnce(OK);
    await act(async () => {
      await result.current.startScan('image', 'file:///a.jpg');
    });
    mockPost.mockRejectedValueOnce(refusal(429, 'daily_scan_limit', {}, { 'retry-after': '60' }));
    await act(async () => {
      await result.current.requestEnhancedScan(result.current.jobs[0].id);
    });
    expect(result.current.jobs[0]).toEqual(
      expect.objectContaining({ status: 'complete', enhanced: false })
    );
    expect(result.current.jobs[0].results).toHaveLength(1);
    expect(result.current.reviewingJob?.id).toBe(result.current.jobs[0].id);
    expect(result.current.scanNotice?.kind).toBe('daily_limit');
  });

  it('daily limit without Retry-After resets at the next UTC midnight', async () => {
    const { result } = await setup();
    mockPost.mockRejectedValueOnce(refusal(429, 'daily_scan_limit'));
    await act(async () => {
      await result.current.startScan('image', 'file:///a.jpg');
    });
    const n = result.current.scanNotice as { resetsAt: number };
    const d = new Date();
    expect(n.resetsAt).toBe(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + 1));
  });

  it('waits the 2 s default when scan_in_progress has no Retry-After header', async () => {
    const { result } = await setup();
    jest.useFakeTimers();
    mockPost.mockRejectedValueOnce(refusal(429, 'scan_in_progress')).mockResolvedValueOnce(OK);
    let started: Promise<void> | void;
    await act(async () => {
      started = result.current.startScan('image', 'file:///a.jpg');
      await jest.advanceTimersByTimeAsync(1999);
    });
    expect(mockPost).toHaveBeenCalledTimes(1);
    await act(async () => {
      await jest.advanceTimersByTimeAsync(1);
      await started;
    });
    expect(mockPost).toHaveBeenCalledTimes(2);
  });

  it('stops draining on the daily limit and leaves the rest queued', async () => {
    const queued = [1, 2, 3].map((i) => ({
      id: `q-${i}`,
      type: 'image' as const,
      status: 'queued' as const,
      createdAt: i,
      imageUri: `file:///q${i}.jpg`,
      retryCount: 0,
    }));
    mockLoadJobs.mockResolvedValue(queued);
    mockPost.mockRejectedValue(refusal(429, 'daily_scan_limit', {}, { 'retry-after': '60' }));
    const { result } = await setup();
    jest.useFakeTimers();
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      // Well past the 2 s delay between drained jobs.
      await jest.advanceTimersByTimeAsync(10000);
    });
    expect(result.current.scanNotice?.kind).toBe('daily_limit');
    expect(mockPost).toHaveBeenCalledTimes(1);
    const statuses = result.current.jobs.map((j) => j.status).sort();
    expect(statuses).toEqual(['queued', 'queued', 'queued']);
  });

  it('an enhanced-only switch-off still lets queued standard photos through', async () => {
    mockLoadJobs.mockResolvedValue([
      {
        id: 'q-enh',
        type: 'image' as const,
        status: 'queued' as const,
        createdAt: 1,
        imageUri: 'file:///e.jpg',
        retryCount: 0,
        enhanced: true,
      },
      {
        id: 'q-enh-2',
        type: 'image' as const,
        status: 'queued' as const,
        createdAt: 2,
        imageUri: 'file:///e2.jpg',
        retryCount: 0,
        enhanced: true,
      },
      {
        id: 'q-std',
        type: 'image' as const,
        status: 'queued' as const,
        createdAt: 3,
        imageUri: 'file:///s.jpg',
        retryCount: 0,
      },
    ]);
    mockPost.mockRejectedValueOnce(refusal(503, 'enhanced_scan_disabled')).mockResolvedValue(OK);
    const { result } = await setup();
    jest.useFakeTimers();
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(10000);
    });
    // The refused enhanced photo, then the standard one; the second enhanced
    // photo is skipped.
    expect(mockPost).toHaveBeenCalledTimes(2);
    const byId = Object.fromEntries(result.current.jobs.map((j) => [j.id, j.status]));
    expect(byId['q-enh']).toBe('queued');
    expect(byId['q-enh-2']).toBe('queued');
    expect(byId['q-std']).toBe('complete');
  });

  it.each([
    ['403 enhanced_requires_premium', refusal(403, 'enhanced_requires_premium')],
    ['429 enhanced_throttled', refusal(429, 'enhanced_throttled', {}, { 'retry-after': '60' })],
  ])('%s still lets queued standard photos through', async (_name, err) => {
    mockLoadJobs.mockResolvedValue([
      {
        id: 'q-enh',
        type: 'image' as const,
        status: 'queued' as const,
        createdAt: 1,
        imageUri: 'file:///e.jpg',
        retryCount: 0,
        enhanced: true,
      },
      {
        id: 'q-enh-2',
        type: 'image' as const,
        status: 'queued' as const,
        createdAt: 2,
        imageUri: 'file:///e2.jpg',
        retryCount: 0,
        enhanced: true,
      },
      {
        id: 'q-std',
        type: 'image' as const,
        status: 'queued' as const,
        createdAt: 3,
        imageUri: 'file:///s.jpg',
        retryCount: 0,
      },
    ]);
    mockPost.mockRejectedValueOnce(err).mockResolvedValue(OK);
    const { result } = await setup();
    jest.useFakeTimers();
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(10000);
    });
    // The refused enhanced photo, then the standard one; the second enhanced
    // photo is not sent in this drain.
    expect(mockPost).toHaveBeenCalledTimes(2);
    const byId = Object.fromEntries(result.current.jobs.map((j) => [j.id, j.status]));
    expect(byId['q-enh']).toBe('failed');
    expect(byId['q-enh-2']).toBe('queued');
    expect(byId['q-std']).toBe('complete');
    expect(result.current.scanNotice).toBeNull();
  });

  it('a photo refusal still lets queued text searches through', async () => {
    const { api } = jest.requireMock('../../lib/api') as { api: { get: jest.Mock } };
    api.get.mockResolvedValue({ data: [] });
    mockLoadJobs.mockResolvedValue([
      {
        id: 'q-img',
        type: 'image' as const,
        status: 'queued' as const,
        createdAt: 1,
        imageUri: 'file:///q.jpg',
        retryCount: 0,
      },
      {
        id: 'q-img-2',
        type: 'image' as const,
        status: 'queued' as const,
        createdAt: 2,
        imageUri: 'file:///q2.jpg',
        retryCount: 0,
      },
      {
        id: 'q-text',
        type: 'text' as const,
        status: 'queued' as const,
        createdAt: 3,
        query: 'Dune',
        retryCount: 0,
      },
    ]);
    mockPost.mockRejectedValue(refusal(429, 'daily_scan_limit', {}, { 'retry-after': '60' }));
    const { result } = await setup();
    jest.useFakeTimers();
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(10000);
    });
    // One refused photo, the second photo skipped, the text search sent.
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(api.get).toHaveBeenCalledWith('/books/search', expect.anything());
    const byId = Object.fromEntries(result.current.jobs.map((j) => [j.id, j.status]));
    expect(byId['q-img']).toBe('queued');
    expect(byId['q-img-2']).toBe('queued');
    expect(byId['q-text']).not.toBe('queued');
  });
});

describe('drain races', () => {
  const queuedJob = (i: number) => ({
    id: `q-${i}`,
    type: 'image' as const,
    status: 'queued' as const,
    createdAt: i,
    imageUri: `file:///q${i}.jpg`,
    retryCount: 0,
  });

  it('a reconnect drain does not send a job startScan is still checking connectivity for', async () => {
    const { result } = await setup();
    let release: (v: { isConnected: boolean }) => void = () => {};
    mockNetFetch.mockImplementationOnce(() => new Promise((res) => (release = res)));
    mockPost.mockResolvedValue(OK);
    let started: Promise<void> | void;
    await act(async () => {
      started = result.current.startScan('image', 'file:///a.jpg');
    });
    // Job is pending, NetInfo.fetch is still unresolved; reconnect fires.
    await act(async () => {
      mockNetListener?.({ isConnected: true });
    });
    await act(async () => {
      release({ isConnected: true });
      await started;
    });
    expect(mockPost).toHaveBeenCalledTimes(1);
  });

  it('skips a queued job dismissed mid-drain', async () => {
    mockLoadJobs.mockResolvedValue([queuedJob(1), queuedJob(2)]);
    mockPost.mockResolvedValue(OK);
    const { result } = await setup();
    jest.useFakeTimers();
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(100);
    });
    expect(mockPost).toHaveBeenCalledTimes(1);
    // Job 2 is waiting out the delay between drained jobs; the user dismisses it.
    await act(async () => {
      result.current.dismissJob('q-2');
    });
    await act(async () => {
      await jest.advanceTimersByTimeAsync(10000);
    });
    expect(mockPost).toHaveBeenCalledTimes(1);
  });
});

describe('Try again resumes the queue', () => {
  const queuedJob = (i: number, extra = {}) => ({
    id: `q-${i}`,
    type: 'image' as const,
    status: 'queued' as const,
    createdAt: i,
    imageUri: `file:///q${i}.jpg`,
    retryCount: 0,
    ...extra,
  });
  const three = () => [1, 2, 3].map((i) => queuedJob(i));

  it('resumes the remaining queued jobs after a drain stopped on a refusal', async () => {
    mockLoadJobs.mockResolvedValue(three());
    mockPost.mockRejectedValueOnce(refusal(429, 'daily_scan_limit', {}, { 'retry-after': '60' }));
    const { result } = await setup();
    jest.useFakeTimers();
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(10000);
    });
    expect(mockPost).toHaveBeenCalledTimes(1);
    mockPost.mockResolvedValue(OK);
    await act(async () => {
      result.current.resumeQueue();
      await jest.advanceTimersByTimeAsync(10000);
    });
    // The refused job again plus the two that were left waiting.
    expect(mockPost).toHaveBeenCalledTimes(4);
    expect(result.current.jobs.map((j) => j.status)).toEqual(['complete', 'complete', 'complete']);
    expect(result.current.scanNotice).toBeNull();
  });

  it('a still-refused Try again sends exactly one request and stops', async () => {
    mockLoadJobs.mockResolvedValue(three());
    mockPost.mockRejectedValue(refusal(503, 'scan_disabled'));
    const { result } = await setup();
    jest.useFakeTimers();
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(10000);
    });
    expect(mockPost).toHaveBeenCalledTimes(1);
    await act(async () => {
      result.current.resumeQueue();
      await jest.advanceTimersByTimeAsync(10000);
    });
    expect(mockPost).toHaveBeenCalledTimes(2);
    expect(result.current.jobs.map((j) => j.status)).toEqual(['queued', 'queued', 'queued']);
    expect(result.current.scanNotice?.kind).toBe('paused');
  });

  it('after a restart, a reconnect drain picks refused jobs up', async () => {
    mockLoadJobs.mockResolvedValue([queuedJob(1, { error: 'scan_limit_reached' })]);
    mockPost.mockResolvedValue(OK);
    const { result } = await setup();
    await act(async () => {
      mockNetListener?.({ isConnected: true });
    });
    await waitFor(() => expect(result.current.jobs[0].status).toBe('complete'));
    expect(mockPost).toHaveBeenCalledTimes(1);
  });

  it('resumeQueue during a running drain does not start a second drain', async () => {
    mockLoadJobs.mockResolvedValue([queuedJob(1), queuedJob(2)]);
    let release: (v: unknown) => void = () => {};
    mockPost.mockImplementationOnce(() => new Promise((res) => (release = res)));
    mockPost.mockResolvedValue(OK);
    const { result } = await setup();
    await act(async () => {
      mockNetListener?.({ isConnected: true });
    });
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    jest.useFakeTimers();
    await act(async () => {
      result.current.resumeQueue();
      await jest.advanceTimersByTimeAsync(100);
    });
    // Job 1 is still in flight; a second drain would have sent job 2 already.
    expect(mockPost).toHaveBeenCalledTimes(1);
    await act(async () => {
      release(OK);
      await jest.advanceTimersByTimeAsync(10000);
    });
    expect(mockPost).toHaveBeenCalledTimes(2);
  });

  it('a refused job that had used retries ends with a fresh retry budget', async () => {
    mockLoadJobs.mockResolvedValue([queuedJob(1, { retryCount: 2 })]);
    mockPost.mockRejectedValue(refusal(503, 'scan_disabled'));
    const { result } = await setup();
    await act(async () => {
      mockNetListener?.({ isConnected: true });
    });
    await waitFor(() => expect(result.current.scanNotice?.kind).toBe('paused'));
    expect(result.current.jobs[0]).toEqual(
      expect.objectContaining({ status: 'queued', retryCount: 0 })
    );
  });

  it('a later successful scan sends the earlier refused photo automatically', async () => {
    const { result } = await setup();
    mockPost.mockRejectedValueOnce(refusal(503, 'scan_disabled'));
    await act(async () => {
      await result.current.startScan('image', 'file:///a.jpg');
    });
    expect(result.current.jobs[0].status).toBe('queued');
    mockPost.mockResolvedValue(OK);
    await act(async () => {
      await result.current.startScan('image', 'file:///b.jpg');
    });
    // A (refused), B (ok), then A again from the automatic drain.
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(3));
    await waitFor(() =>
      expect(result.current.jobs.map((j) => j.status)).toEqual(['complete', 'complete'])
    );
    expect(mockPost).toHaveBeenCalledTimes(3);
  });
});
