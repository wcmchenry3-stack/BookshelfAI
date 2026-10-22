import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react-native';

import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { BannerProvider } from '../../contexts/BannerContext';
import { useScanJobs } from '../../hooks/useScanJobs';

const mockPost = jest.fn();
jest.mock('../../lib/api', () => ({
  api: { post: (...a: unknown[]) => mockPost(...a), get: jest.fn() },
}));

const mockSaveJobs = jest.fn();
const mockLoadJobs = jest.fn();
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
    fetch: () => Promise.resolve({ isConnected: true }),
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
    enhanced_scan_credits: 5,
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
});

afterEach(() => {
  jest.useRealTimers();
});

describe('scan refusals', () => {
  it('402 scan_limit_reached sets the monthly notice and stops the job', async () => {
    const { result } = await setup();
    mockPost.mockRejectedValueOnce(
      refusal(402, 'scan_limit_reached', { limit: 5, used: 5, period_end: '2026-11-01' })
    );
    await act(async () => {
      await result.current.startScan('image', 'file:///a.jpg');
    });
    expect(result.current.scanNotice).toEqual({
      kind: 'monthly_limit',
      limit: 5,
      periodEnd: '2026-11-01',
      jobId: result.current.jobs[0].id,
    });
    expect(result.current.jobs[0].status).toBe('failed');
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
        jobId: result.current.jobs[0].id,
      });
    }
  );

  it('keeps the enhanced out-of-credits handling for 402 no_enhanced_credits', async () => {
    const { result } = await setup();
    mockPost.mockResolvedValueOnce({ data: { ...OK.data, books: [] } });
    await act(async () => {
      await result.current.startScan('image', 'file:///a.jpg');
    });
    mockPost.mockRejectedValueOnce(refusal(402, 'no_enhanced_credits'));
    await act(async () => {
      await result.current.requestEnhancedScan(result.current.jobs[0].id);
    });
    expect(result.current.enhancedCredits).toBe(0);
    expect(result.current.scanNotice).toBeNull();
  });

  it('a successful scan clears the notice and bumps the usage refresh key', async () => {
    const { result } = await setup();
    mockPost.mockRejectedValueOnce(refusal(503, 'scan_disabled'));
    await act(async () => {
      await result.current.startScan('image', 'file:///a.jpg');
    });
    const before = result.current.usageRefreshKey;
    mockPost.mockResolvedValueOnce(OK);
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
      jobId: result.current.jobs[0].id,
    });
    expect(result.current.jobs[0].status).toBe('failed');
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

  it('the notice carries the refused job id so it can be retried', async () => {
    const { result } = await setup();
    mockPost.mockRejectedValueOnce(refusal(503, 'scan_disabled'));
    await act(async () => {
      await result.current.startScan('image', 'file:///a.jpg');
    });
    const jobId = result.current.jobs[0].id;
    expect((result.current.scanNotice as { jobId?: string }).jobId).toBe(jobId);
    mockPost.mockResolvedValueOnce(OK);
    await act(async () => {
      await result.current.retryScan(jobId);
    });
    expect(result.current.jobs[0].status).toBe('complete');
  });

  it('a refused enhanced re-scan restores earlier results silently and shows the notice', async () => {
    const { result } = await setup();
    mockPost.mockResolvedValueOnce(OK);
    await act(async () => {
      await result.current.startScan('image', 'file:///a.jpg');
    });
    mockPost.mockRejectedValueOnce(refusal(402, 'scan_limit_reached', { limit: 5 }));
    await act(async () => {
      await result.current.requestEnhancedScan(result.current.jobs[0].id);
    });
    expect(result.current.jobs[0]).toEqual(
      expect.objectContaining({ status: 'complete', enhanced: false })
    );
    expect(result.current.jobs[0].results).toHaveLength(1);
    expect(result.current.reviewingJob?.id).toBe(result.current.jobs[0].id);
    expect(result.current.scanNotice?.kind).toBe('monthly_limit');
    // Restored jobs are not retryable from the notice.
    expect(result.current.scanNotice?.jobId).toBeUndefined();
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

  it('stops draining on a monthly limit and leaves the rest queued', async () => {
    const queued = [1, 2, 3].map((i) => ({
      id: `q-${i}`,
      type: 'image' as const,
      status: 'queued' as const,
      createdAt: i,
      imageUri: `file:///q${i}.jpg`,
      retryCount: 0,
    }));
    mockLoadJobs.mockResolvedValue(queued);
    mockPost.mockRejectedValue(refusal(402, 'scan_limit_reached', { limit: 5 }));
    const { result } = await setup();
    jest.useFakeTimers();
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      // Well past the 2 s delay between drained jobs.
      await jest.advanceTimersByTimeAsync(10000);
    });
    expect(result.current.scanNotice?.kind).toBe('monthly_limit');
    expect(mockPost).toHaveBeenCalledTimes(1);
    const statuses = result.current.jobs.map((j) => j.status).sort();
    expect(statuses).toEqual(['failed', 'queued', 'queued']);
  });
});
