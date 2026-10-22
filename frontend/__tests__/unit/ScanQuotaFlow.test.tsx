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
jest.mock('../../lib/scanJobStorage', () => ({
  loadJobs: () => Promise.resolve([]),
  saveJobs: (...a: unknown[]) => mockSaveJobs(...a),
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
  });

  it.each(['scan_disabled', 'scan_capacity', 'ai_unavailable', 'scan_unavailable'])(
    '503 %s sets the paused notice',
    async (code) => {
      const { result } = await setup();
      mockPost.mockRejectedValueOnce(refusal(503, code));
      await act(async () => {
        await result.current.startScan('image', 'file:///a.jpg');
      });
      expect(result.current.scanNotice).toEqual({ kind: 'paused' });
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
    expect(result.current.scanNotice).toEqual({ kind: 'still_processing' });
    expect(result.current.jobs[0].status).toBe('failed');
  });
});
