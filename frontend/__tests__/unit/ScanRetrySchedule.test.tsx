import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react-native';

import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { BannerProvider } from '../../contexts/BannerContext';
import { useBanner } from '../../hooks/useBanner';
import { useScanJobs } from '../../hooks/useScanJobs';
import { SCAN_RETRY_DELAYS_MS, MAX_SCAN_ATTEMPTS } from '../../lib/scanErrors';
import type { ScanJob } from '../../lib/scanJob';

// #604: a photo the server could not scan because the AI was unavailable
// (503 scan_unavailable, including a malformed reply, #572) is retried on a
// fixed schedule, one photo at a time, and then failed.

const mockPost = jest.fn();
const mockGet = jest.fn();
jest.mock('../../lib/api', () => ({
  api: {
    post: (...a: unknown[]) => mockPost(...a),
    get: (...a: unknown[]) => mockGet(...a),
  },
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

const mockCaptureMessage = jest.fn();
jest.mock('../../lib/sentry', () => ({
  Sentry: {
    addBreadcrumb: jest.fn(),
    captureException: jest.fn(),
    captureMessage: (...a: unknown[]) => mockCaptureMessage(...a),
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

function refusal(status: number, detail: string) {
  return { response: { status, data: { detail }, headers: {} } };
}
const UNAVAILABLE = refusal(503, 'scan_unavailable');
const NETWORK_ERROR = Object.assign(new Error('Network Error'), { response: undefined });

const OK = {
  data: {
    books: [{ title: 'Dune', author: 'Herbert' }],
    enhanced: false,
    enhanced_scans_remaining: null,
  },
};

/** The `attempt` form field of the n-th POST /scan. */
function attemptOf(n: number): unknown {
  const form = mockPost.mock.calls[n][1] as FormData & { _parts?: [string, unknown][] };
  if (typeof form.get === 'function') return form.get('attempt') ?? undefined;
  return Object.fromEntries(form._parts ?? []).attempt;
}
function attempts(): unknown[] {
  return mockPost.mock.calls.map((_c, i) => attemptOf(i));
}

function queuedJob(i: number, extra: Partial<ScanJob> = {}): ScanJob {
  return {
    id: `q-${i}`,
    type: 'image',
    status: 'queued',
    createdAt: i,
    imageUri: `file:///q${i}.jpg`,
    retryCount: 0,
    ...extra,
  };
}

async function setup() {
  const hook = await renderHook(() => ({ scan: useScanJobs(), banner: useBanner() }), {
    wrapper,
  });
  await waitFor(() => expect(mockSaveJobs).toHaveBeenCalled());
  return hook;
}

async function advance(ms: number) {
  await act(async () => {
    await jest.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  jest.resetAllMocks();
  jest.useFakeTimers();
  mockUuid = 0;
  mockNetListener = null;
  mockSaveJobs.mockResolvedValue(undefined);
  mockLoadJobs.mockResolvedValue([]);
  mockNetFetch.mockResolvedValue({ isConnected: true });
});

afterEach(() => {
  jest.useRealTimers();
});

describe('the retry schedule (#604)', () => {
  it('keeps the delays in one place: 30 s, 10 min, 1 h, 4 h, then failed after attempt 5', () => {
    expect(SCAN_RETRY_DELAYS_MS).toEqual([30_000, 600_000, 3_600_000, 14_400_000]);
    expect(MAX_SCAN_ATTEMPTS).toBe(5);
  });

  it('retries after 30 s, 10 min, 1 h and 4 h, sending attempt 1-5', async () => {
    mockPost.mockRejectedValue(UNAVAILABLE);
    const { result } = await setup();
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///a.jpg');
    });
    expect(mockPost).toHaveBeenCalledTimes(1);

    const delays = [30_000, 600_000, 3_600_000, 14_400_000];
    for (const [i, delay] of delays.entries()) {
      await advance(delay - 1);
      expect(mockPost).toHaveBeenCalledTimes(i + 1);
      await advance(1);
      expect(mockPost).toHaveBeenCalledTimes(i + 2);
    }
    expect(attempts()).toEqual(['1', '2', '3', '4', '5']);
    // Nothing more after attempt 5.
    await advance(24 * 3_600_000);
    expect(mockPost).toHaveBeenCalledTimes(5);
  });

  it('stores the attempt count and the next try time on the queued job, and saves them', async () => {
    mockPost.mockRejectedValue(UNAVAILABLE);
    const { result } = await setup();
    const before = Date.now();
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///a.jpg');
    });
    const job = result.current.scan.jobs[0];
    expect(job).toEqual(
      expect.objectContaining({
        status: 'queued',
        error: 'unavailable',
        attempts: 1,
        nextAttemptAt: before + 30_000,
      })
    );
    // Still counted as waiting to upload.
    expect(result.current.scan.pendingCount).toBe(1);
    const saved = mockSaveJobs.mock.calls.at(-1)?.[0] as ScanJob[];
    expect(saved[0]).toEqual(
      expect.objectContaining({ attempts: 1, nextAttemptAt: before + 30_000 })
    );
  });

  it('shows the short notice only while the first retry is pending', async () => {
    mockPost.mockRejectedValue(UNAVAILABLE);
    const { result } = await setup();
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///a.jpg');
    });
    expect(result.current.scan.scanNotice).toEqual({ kind: 'paused' });
    await advance(30_000);
    expect(mockPost).toHaveBeenCalledTimes(2);
    expect(result.current.scan.scanNotice).toBeNull();
    expect(result.current.scan.jobs[0].attempts).toBe(2);
  });

  it('a success on a retry ends the schedule', async () => {
    mockPost.mockRejectedValueOnce(UNAVAILABLE).mockResolvedValue(OK);
    const { result } = await setup();
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///a.jpg');
    });
    await advance(30_000);
    const job = result.current.scan.jobs[0];
    expect(job.status).toBe('complete');
    expect(job.attempts).toBeUndefined();
    expect(job.nextAttemptAt).toBeUndefined();
    expect(result.current.scan.scanNotice).toBeNull();
  });
});

describe('only provider-unavailable outcomes are retried (#604)', () => {
  it.each([
    ['503 scan_disabled', refusal(503, 'scan_disabled')],
    ['503 ai_budget_reached', refusal(503, 'ai_budget_reached')],
    ['503 ai_unavailable', refusal(503, 'ai_unavailable')],
    ['503 scan_capacity', refusal(503, 'scan_capacity')],
    ['429 daily_scan_limit', refusal(429, 'daily_scan_limit')],
    ['429 scan_requests_daily_limit', refusal(429, 'scan_requests_daily_limit')],
    ['401', refusal(401, 'not_authenticated')],
    ['500', refusal(500, 'boom')],
  ])('%s is not retried', async (_name, err) => {
    mockPost.mockRejectedValue(err);
    const { result } = await setup();
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///a.jpg');
    });
    await advance(5 * 3_600_000);
    // One request, sent as attempt 1, and no retry.
    expect(attempts()).toEqual(['1']);
    expect(result.current.scan.jobs[0].nextAttemptAt).toBeUndefined();
    expect(result.current.scan.jobs[0].error).not.toBe('unavailable');
  });

  it('no books found is not retried', async () => {
    mockPost.mockResolvedValue({ data: { books: [], enhanced: false, no_match: 'free' } });
    const { result } = await setup();
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///a.jpg');
    });
    await advance(5 * 3_600_000);
    expect(attempts()).toEqual(['1']);
    expect(result.current.scan.jobs[0].status).toBe('failed');
  });

  it('a refusal on a later attempt ends the schedule', async () => {
    mockPost.mockRejectedValueOnce(UNAVAILABLE).mockRejectedValue(refusal(503, 'scan_disabled'));
    const { result } = await setup();
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///a.jpg');
    });
    await advance(30_000);
    expect(mockPost).toHaveBeenCalledTimes(2);
    const job = result.current.scan.jobs[0];
    expect(job).toEqual(expect.objectContaining({ status: 'queued', error: 'paused' }));
    expect(job.nextAttemptAt).toBeUndefined();
    expect(job.attempts).toBeUndefined();
    await advance(5 * 3_600_000);
    expect(mockPost).toHaveBeenCalledTimes(2);
  });
});

describe('offline pauses the schedule (#604)', () => {
  it('sends nothing while offline and sends the due photo on reconnect', async () => {
    mockPost.mockRejectedValueOnce(UNAVAILABLE).mockResolvedValue(OK);
    const { result } = await setup();
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///a.jpg');
    });
    mockNetFetch.mockResolvedValue({ isConnected: false });
    await advance(2 * 3_600_000);
    expect(mockPost).toHaveBeenCalledTimes(1);
    // No attempt used while offline.
    expect(result.current.scan.jobs[0].attempts).toBe(1);

    mockNetFetch.mockResolvedValue({ isConnected: true });
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(mockPost).toHaveBeenCalledTimes(2);
    expect(attemptOf(1)).toBe('2');
    expect(result.current.scan.jobs[0].status).toBe('complete');
  });

  it('a network failure on a retry is not a failed attempt', async () => {
    mockPost
      .mockRejectedValueOnce(UNAVAILABLE)
      .mockRejectedValueOnce(NETWORK_ERROR)
      .mockResolvedValue(OK);
    const { result } = await setup();
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///a.jpg');
    });
    await advance(30_000);
    expect(mockPost).toHaveBeenCalledTimes(2);
    expect(result.current.scan.jobs[0]).toEqual(
      expect.objectContaining({ status: 'queued', attempts: 1 })
    );
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(mockPost).toHaveBeenCalledTimes(3);
    // The same attempt number again: the lost request did not count.
    expect(attempts()).toEqual(['1', '2', '2']);
  });
});

describe('one photo at a time (#604)', () => {
  it('the rest of the queue waits for the retrying photo, then drains', async () => {
    mockLoadJobs.mockResolvedValue([queuedJob(1), queuedJob(2), queuedJob(3)]);
    mockPost.mockRejectedValueOnce(UNAVAILABLE).mockResolvedValue(OK);
    const { result } = await setup();
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      // Well past the 2 s delay between drained jobs.
      await jest.advanceTimersByTimeAsync(10_000);
    });
    expect(mockPost).toHaveBeenCalledTimes(1);
    const retrying = result.current.scan.jobs.find((j) => j.attempts === 1);
    expect(retrying?.id).toBe('q-1');

    // A reconnect or "Try again" during the wait sends nothing either.
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      result.current.scan.resumeQueue();
      await jest.advanceTimersByTimeAsync(10_000);
    });
    expect(mockPost).toHaveBeenCalledTimes(1);

    await advance(20_000);
    await advance(10_000);
    expect(mockPost).toHaveBeenCalledTimes(4);
    // The retrying photo went first (attempt 2), then the others (attempt 1).
    expect(attempts()).toEqual(['1', '2', '1', '1']);
    expect(result.current.scan.jobs.every((j) => j.status === 'complete')).toBe(true);
  });

  it('a retry that fails again keeps the rest of the queue waiting', async () => {
    mockLoadJobs.mockResolvedValue([queuedJob(1), queuedJob(2)]);
    mockPost.mockRejectedValue(UNAVAILABLE);
    const { result } = await setup();
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(10_000);
    });
    await advance(30_000);
    expect(mockPost).toHaveBeenCalledTimes(2);
    expect(attempts()).toEqual(['1', '2']);
    const job2 = result.current.scan.jobs.find((j) => j.id === 'q-2');
    expect(job2?.status).toBe('queued');
    expect(job2?.attempts).toBeUndefined();
  });
});

describe('the schedule survives a restart (#604)', () => {
  it('honours a saved next try time and attempt count', async () => {
    const now = Date.now();
    mockLoadJobs.mockResolvedValue([
      queuedJob(1, { error: 'unavailable', attempts: 2, nextAttemptAt: now + 60_000 }),
    ]);
    mockPost.mockResolvedValue(OK);
    const { result } = await setup();
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(10_000);
    });
    // Not due yet: the launch drain leaves it alone.
    expect(mockPost).not.toHaveBeenCalled();
    expect(result.current.scan.jobs[0].attempts).toBe(2);

    await advance(50_000);
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(attemptOf(0)).toBe('3');
  });

  it('sends a photo whose wait passed while the app was closed on launch', async () => {
    mockLoadJobs.mockResolvedValue([
      queuedJob(1, { error: 'unavailable', attempts: 3, nextAttemptAt: Date.now() - 1000 }),
    ]);
    mockPost.mockResolvedValue(OK);
    await setup();
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(attemptOf(0)).toBe('4');
  });

  it('a job interrupted mid-retry (searching) is sent again with the same attempt', async () => {
    mockLoadJobs.mockResolvedValue([
      queuedJob(1, {
        status: 'searching',
        error: 'unavailable',
        attempts: 1,
        nextAttemptAt: Date.now() - 1000,
      }),
    ]);
    mockPost.mockResolvedValue(OK);
    await setup();
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(attempts()).toEqual(['2']);
  });
});

describe('final failure (#604)', () => {
  async function failAllAttempts() {
    mockPost.mockRejectedValue(UNAVAILABLE);
    const hook = await setup();
    await act(async () => {
      await hook.result.current.scan.startScan('image', 'file:///docs/scan-queue/secret.jpg');
    });
    for (const delay of SCAN_RETRY_DELAYS_MS) await advance(delay);
    return hook;
  }

  it('moves the photo to failed after attempt 5 with one banner', async () => {
    const { result } = await failAllAttempts();
    expect(mockPost).toHaveBeenCalledTimes(5);
    const job = result.current.scan.jobs[0];
    expect(job).toEqual(
      expect.objectContaining({ status: 'failed', error: 'unavailable', attempts: 5 })
    );
    expect(job.nextAttemptAt).toBeUndefined();
    expect(result.current.scan.pendingCount).toBe(0);
    expect(result.current.banner.banner?.message).toBe(
      'This scan failed. Open the queue to retry or delete it.'
    );
    expect(result.current.banner.banner?.type).toBe('error');
    expect(result.current.scan.scanNotice).toBeNull();
  });

  it('sends one Sentry message with the attempt count and last error, no user content', async () => {
    await failAllAttempts();
    expect(mockCaptureMessage).toHaveBeenCalledTimes(1);
    const [message, context] = mockCaptureMessage.mock.calls[0];
    expect(typeof message).toBe('string');
    expect(context).toEqual(
      expect.objectContaining({
        fingerprint: ['scan-final-failure'],
        tags: expect.objectContaining({ attempts: '5', last_error: 'scan_unavailable' }),
      })
    );
    const sent = JSON.stringify(mockCaptureMessage.mock.calls);
    expect(sent).not.toMatch(/file:|secret|jpg|mockUuid/);
  });

  it('the banner Retry restarts the schedule at attempt 1', async () => {
    const { result } = await failAllAttempts();
    const retry = result.current.banner.banner?.actions?.[0];
    expect(retry?.label).toBe('Retry');
    mockPost.mockReset();
    mockPost.mockRejectedValue(UNAVAILABLE);
    await act(async () => {
      retry?.onPress();
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(attempts()).toEqual(['1']);
    expect(result.current.scan.jobs[0]).toEqual(
      expect.objectContaining({ status: 'queued', attempts: 1 })
    );
    await advance(30_000);
    expect(attempts()).toEqual(['1', '2']);
  });
});

describe('the retry timer (#604)', () => {
  it('is cleared on unmount', async () => {
    mockPost.mockRejectedValue(UNAVAILABLE);
    const { result, unmount } = await setup();
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///a.jpg');
    });
    expect(result.current.scan.jobs[0].nextAttemptAt).toBeDefined();
    await act(async () => {
      await unmount();
    });
    await advance(60_000);
    expect(mockPost).toHaveBeenCalledTimes(1);
  });

  it('does not double-send a job that is still in flight when the timer fires', async () => {
    mockPost.mockRejectedValueOnce(UNAVAILABLE);
    let release: (v: unknown) => void = () => {};
    mockPost.mockImplementationOnce(() => new Promise((res) => (release = res)));
    const { result } = await setup();
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///a.jpg');
    });
    await advance(30_000);
    expect(mockPost).toHaveBeenCalledTimes(2);
    // Attempt 2 is in flight; a reconnect drain and more time must not resend it.
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(60_000);
    });
    expect(mockPost).toHaveBeenCalledTimes(2);
    await act(async () => {
      release(OK);
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(result.current.scan.jobs[0].status).toBe('complete');
    expect(mockPost).toHaveBeenCalledTimes(2);
  });
});
