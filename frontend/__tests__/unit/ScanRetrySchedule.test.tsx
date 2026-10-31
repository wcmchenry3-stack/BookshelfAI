import { act, waitFor } from '@testing-library/react-native';

import type { useScanJobs } from '../../hooks/useScanJobs';
import { renderScanJobsWithBanner } from '../helpers/providers';
import { SCAN_RETRY_DELAYS_MS, MAX_SCAN_ATTEMPTS } from '../../lib/scanErrors';
import type { ScanJob } from '../../lib/scanJob';
import {
  queuedJob as makeQueuedJob,
  refusal,
  resetScanMocks,
  scanMocks,
  scanOk,
} from '../helpers/scanJobMocks';

// #604: a photo the server could not scan because the AI was unavailable
// (503 scan_unavailable, including a malformed reply, #572) is retried on a
// fixed schedule, one photo at a time, and then failed.

jest.mock('../../hooks/useAuth', () => require('../helpers/scanJobMocks').authedModule());
jest.mock('../../lib/api', () => require('../helpers/scanJobMocks').scanApiModule());
jest.mock('../../lib/scanJobStorage', () => require('../helpers/scanJobMocks').scanStorageModule());
jest.mock('@react-native-community/netinfo', () =>
  require('../helpers/scanJobMocks').netInfoModule()
);
jest.mock('../../lib/sentry', () => require('../helpers/scanJobMocks').sentryModule());
jest.mock('expo-crypto', () => require('../helpers/scanJobMocks').cryptoModule());

const {
  post: mockPost,
  get: mockGet,
  saveJobs: mockSaveJobs,
  loadJobs: mockLoadJobs,
  netFetch: mockNetFetch,
  captureMessage: mockCaptureMessage,
} = scanMocks;

const UNAVAILABLE = refusal(503, 'scan_unavailable');
const NETWORK_ERROR = Object.assign(new Error('Network Error'), { response: undefined });

const OK = scanOk(null);

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
  return makeQueuedJob(i, extra) as ScanJob;
}

async function setup() {
  const hook = await renderScanJobsWithBanner();
  await waitFor(() => expect(mockSaveJobs).toHaveBeenCalled());
  return hook;
}

/** Start one image scan inside `act`. */
async function startPhoto(scan: Pick<ReturnType<typeof useScanJobs>, 'startScan'>, uri: string) {
  await act(async () => {
    await scan.startScan('image', uri);
  });
}

async function advance(ms: number) {
  await act(async () => {
    await jest.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  jest.resetAllMocks();
  jest.useFakeTimers();
  resetScanMocks();
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
    await startPhoto(result.current.scan, 'file:///a.jpg');
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
    await startPhoto(result.current.scan, 'file:///a.jpg');
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
    await startPhoto(result.current.scan, 'file:///a.jpg');
    expect(result.current.scan.scanNotice).toEqual({ kind: 'paused' });
    await advance(30_000);
    expect(mockPost).toHaveBeenCalledTimes(2);
    expect(result.current.scan.scanNotice).toBeNull();
    expect(result.current.scan.jobs[0].attempts).toBe(2);
  });

  it('a success on a retry ends the schedule', async () => {
    mockPost.mockRejectedValueOnce(UNAVAILABLE).mockResolvedValue(OK);
    const { result } = await setup();
    await startPhoto(result.current.scan, 'file:///a.jpg');
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
    await startPhoto(result.current.scan, 'file:///a.jpg');
    await advance(5 * 3_600_000);
    // One request, sent as attempt 1, and no retry.
    expect(attempts()).toEqual(['1']);
    expect(result.current.scan.jobs[0].nextAttemptAt).toBeUndefined();
    expect(result.current.scan.jobs[0].error).not.toBe('unavailable');
  });

  it('no books found is not retried', async () => {
    mockPost.mockResolvedValue({ data: { books: [], enhanced: false, no_match: 'free' } });
    const { result } = await setup();
    await startPhoto(result.current.scan, 'file:///a.jpg');
    await advance(5 * 3_600_000);
    expect(attempts()).toEqual(['1']);
    expect(result.current.scan.jobs[0].status).toBe('failed');
  });

  it('a refusal on a later attempt ends the schedule', async () => {
    mockPost.mockRejectedValueOnce(UNAVAILABLE).mockRejectedValue(refusal(503, 'scan_disabled'));
    const { result } = await setup();
    await startPhoto(result.current.scan, 'file:///a.jpg');
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
    await startPhoto(result.current.scan, 'file:///a.jpg');
    mockNetFetch.mockResolvedValue({ isConnected: false });
    await advance(2 * 3_600_000);
    expect(mockPost).toHaveBeenCalledTimes(1);
    // No attempt used while offline.
    expect(result.current.scan.jobs[0].attempts).toBe(1);

    mockNetFetch.mockResolvedValue({ isConnected: true });
    await act(async () => {
      scanMocks.netListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(mockPost).toHaveBeenCalledTimes(2);
    expect(attemptOf(1)).toBe('2');
    expect(result.current.scan.jobs[0].status).toBe('complete');
  });

  it('a network failure on a retry is not a failed attempt, and waits 30 s (no tight loop)', async () => {
    // Codex B: NetInfo still says connected, every request gets no response.
    mockPost.mockRejectedValueOnce(UNAVAILABLE).mockRejectedValue(NETWORK_ERROR);
    const { result } = await setup();
    await startPhoto(result.current.scan, 'file:///a.jpg');
    await advance(30_000);
    expect(mockPost).toHaveBeenCalledTimes(2);
    const before = Date.now();
    expect(result.current.scan.jobs[0]).toEqual(
      expect.objectContaining({ status: 'queued', attempts: 1, nextAttemptAt: before + 30_000 })
    );
    // Not resent at once, nor on a reconnect event, before the 30 s floor.
    await act(async () => {
      scanMocks.netListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(29_000);
    });
    expect(mockPost).toHaveBeenCalledTimes(2);

    mockPost.mockResolvedValue(OK);
    await advance(1_000);
    expect(mockPost).toHaveBeenCalledTimes(3);
    // The same attempt number again: the lost request did not count.
    expect(attempts()).toEqual(['1', '2', '2']);
    expect(result.current.scan.jobs[0].status).toBe('complete');
  });
});

describe('one photo at a time (#604)', () => {
  it('the rest of the queue waits for the retrying photo, then drains', async () => {
    mockLoadJobs.mockResolvedValue([queuedJob(1), queuedJob(2), queuedJob(3)]);
    mockPost.mockRejectedValueOnce(UNAVAILABLE).mockResolvedValue(OK);
    const { result } = await setup();
    await act(async () => {
      scanMocks.netListener?.({ isConnected: true });
      // Well past the 2 s delay between drained jobs.
      await jest.advanceTimersByTimeAsync(10_000);
    });
    expect(mockPost).toHaveBeenCalledTimes(1);
    const retrying = result.current.scan.jobs.find((j) => j.attempts === 1);
    expect(retrying?.id).toBe('q-1');

    // A reconnect or "Try again" during the wait sends nothing either.
    await act(async () => {
      scanMocks.netListener?.({ isConnected: true });
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
      scanMocks.netListener?.({ isConnected: true });
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
      scanMocks.netListener?.({ isConnected: true });
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
      scanMocks.netListener?.({ isConnected: true });
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
      scanMocks.netListener?.({ isConnected: true });
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
    expect(result.current.banner.banner?.message).toBe('This scan failed. Tap Retry to try again.');
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
    await startPhoto(result.current.scan, 'file:///a.jpg');
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
    await startPhoto(result.current.scan, 'file:///a.jpg');
    await advance(30_000);
    expect(mockPost).toHaveBeenCalledTimes(2);
    // Attempt 2 is in flight; a reconnect drain and more time must not resend it.
    await act(async () => {
      scanMocks.netListener?.({ isConnected: true });
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

describe('a photo that fails for good releases the queue (#604, Codex A)', () => {
  it('after attempt 5 the rest of the queue is sent', async () => {
    mockLoadJobs.mockResolvedValue([queuedJob(1), queuedJob(2), queuedJob(3)]);
    for (let i = 0; i < MAX_SCAN_ATTEMPTS; i++) mockPost.mockRejectedValueOnce(UNAVAILABLE);
    mockPost.mockResolvedValue(OK);
    const { result } = await setup();
    await act(async () => {
      scanMocks.netListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(10_000);
    });
    for (const delay of SCAN_RETRY_DELAYS_MS) await advance(delay);
    await advance(10_000);
    expect(mockPost).toHaveBeenCalledTimes(7);
    expect(attempts()).toEqual(['1', '2', '3', '4', '5', '1', '1']);
    const byId = Object.fromEntries(result.current.scan.jobs.map((j) => [j.id, j.status]));
    expect(byId).toEqual({ 'q-1': 'failed', 'q-2': 'complete', 'q-3': 'complete' });
  });
});

describe('a success brings waiting photos forward (#604)', () => {
  it('a new capture during a wait is sent at once, and its success sends the waiting photo now', async () => {
    mockPost.mockRejectedValueOnce(UNAVAILABLE).mockResolvedValue(OK);
    const { result } = await setup();
    await startPhoto(result.current.scan, 'file:///a.jpg');
    await advance(5_000);
    await startPhoto(result.current.scan, 'file:///b.jpg');
    await advance(100);
    // a (attempt 1), b (attempt 1, straight away), a again well before its 30 s.
    expect(attempts()).toEqual(['1', '1', '2']);
    expect(result.current.scan.jobs.every((j) => j.status === 'complete')).toBe(true);
  });

  it('a drained photo that succeeds brings the other waiting photos forward', async () => {
    const now = Date.now();
    mockLoadJobs.mockResolvedValue([
      queuedJob(1, { error: 'unavailable', attempts: 1, nextAttemptAt: now - 1000 }),
      queuedJob(2, { error: 'unavailable', attempts: 3, nextAttemptAt: now + 3_600_000 }),
    ]);
    mockPost.mockResolvedValue(OK);
    const { result } = await setup();
    await act(async () => {
      scanMocks.netListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(10_000);
    });
    // q-2 keeps its attempt count: attempt 4, not 1.
    expect(attempts()).toEqual(['2', '4']);
    expect(result.current.scan.jobs.every((j) => j.status === 'complete')).toBe(true);
  });

  it('a new capture that also gets 503 starts its own schedule; the notice stays until its first retry', async () => {
    mockPost.mockRejectedValue(UNAVAILABLE);
    const { result } = await setup();
    await startPhoto(result.current.scan, 'file:///a.jpg');
    await advance(5_000);
    await startPhoto(result.current.scan, 'file:///b.jpg');
    expect(attempts()).toEqual(['1', '1']);
    expect(result.current.scan.scanNotice).toEqual({ kind: 'paused' });
    const [b, a] = result.current.scan.jobs;
    expect(a.nextAttemptAt! + 5_000).toBe(b.nextAttemptAt);

    // a's attempt 2 at 30 s: b's first retry is still pending, so the notice stays.
    await advance(25_000);
    expect(attempts()).toEqual(['1', '1', '2']);
    expect(result.current.scan.scanNotice).toEqual({ kind: 'paused' });
    // b's attempt 2 at 35 s: no first retry pending any more.
    await advance(5_000);
    expect(attempts()).toEqual(['1', '1', '2', '2']);
    expect(result.current.scan.scanNotice).toBeNull();
  });
});

describe('enhanced re-scans keep the paused path (#604)', () => {
  it('an enhanced job with no earlier results is queued as paused, not scheduled', async () => {
    mockPost
      .mockResolvedValueOnce({
        data: { books: [], enhanced: false, no_match: 'counted', enhanced_scans_remaining: 3 },
      })
      .mockRejectedValue(UNAVAILABLE);
    const { result } = await setup();
    await startPhoto(result.current.scan, 'file:///a.jpg');
    const id = result.current.scan.jobs[0].id;
    await act(async () => {
      await result.current.scan.requestEnhancedScan(id);
    });
    const job = result.current.scan.jobs[0];
    expect(job).toEqual(expect.objectContaining({ status: 'queued', error: 'paused' }));
    expect(job.nextAttemptAt).toBeUndefined();
    expect(job.attempts).toBeUndefined();
    expect(result.current.scan.scanNotice).toEqual({ kind: 'paused' });
    await advance(5 * 3_600_000);
    expect(mockPost).toHaveBeenCalledTimes(2);
    expect(mockCaptureMessage).not.toHaveBeenCalled();
    expect(result.current.banner.banner?.message).not.toBe(
      'This scan failed. Tap Retry to try again.'
    );
  });
});

describe('connectivity on the timer (#604)', () => {
  it('an unknown connection state (isConnected null) counts as online', async () => {
    mockPost.mockRejectedValueOnce(UNAVAILABLE).mockResolvedValue(OK);
    const { result } = await setup();
    await startPhoto(result.current.scan, 'file:///a.jpg');
    mockNetFetch.mockResolvedValue({ isConnected: null });
    await advance(30_000);
    expect(attempts()).toEqual(['1', '2']);
    expect(result.current.scan.jobs[0].status).toBe('complete');
  });

  it('offline when the timer fires: checks again after 60 s without a reconnect event', async () => {
    mockPost.mockRejectedValueOnce(UNAVAILABLE).mockResolvedValue(OK);
    const { result } = await setup();
    await startPhoto(result.current.scan, 'file:///a.jpg');
    mockNetFetch.mockResolvedValue({ isConnected: false });
    await advance(30_000);
    expect(mockPost).toHaveBeenCalledTimes(1);
    // Back online, but no NetInfo event arrives.
    mockNetFetch.mockResolvedValue({ isConnected: true });
    await advance(59_000);
    expect(mockPost).toHaveBeenCalledTimes(1);
    await advance(1_000);
    expect(attempts()).toEqual(['1', '2']);
    expect(result.current.scan.jobs[0].status).toBe('complete');
  });
});

describe('the wait is capped at 4 h (#604)', () => {
  it('a saved next try time further ahead is brought forward on load', async () => {
    const now = Date.now();
    mockLoadJobs.mockResolvedValue([
      queuedJob(1, { error: 'unavailable', attempts: 1, nextAttemptAt: now + 10 * 24 * 3_600_000 }),
    ]);
    mockPost.mockResolvedValue(OK);
    const { result } = await setup();
    expect(result.current.scan.jobs[0].nextAttemptAt).toBeLessThanOrEqual(
      Date.now() + 4 * 3_600_000
    );
    await act(async () => {
      scanMocks.netListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(10_000);
    });
    expect(mockPost).not.toHaveBeenCalled();
    await advance(4 * 3_600_000);
    expect(attempts()).toEqual(['2']);
  });
});

describe('the drain while a photo waits (#604)', () => {
  const TEXT_OK = { data: [{ title: 'Dune', author: 'Herbert' }] };
  function textJob(i: number): ScanJob {
    return {
      id: `t-${i}`,
      type: 'text',
      status: 'queued',
      createdAt: 100 + i,
      query: `book ${i}`,
      retryCount: 0,
    };
  }

  it('a queued text search still goes out', async () => {
    mockLoadJobs.mockResolvedValue([
      queuedJob(1, { error: 'unavailable', attempts: 1, nextAttemptAt: Date.now() + 60_000 }),
      textJob(1),
    ]);
    mockGet.mockResolvedValue(TEXT_OK);
    const { result } = await setup();
    await act(async () => {
      scanMocks.netListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(10_000);
    });
    expect(mockGet).toHaveBeenCalledTimes(1);
    expect(mockPost).not.toHaveBeenCalled();
    expect(result.current.scan.jobs.find((j) => j.id === 't-1')?.status).toBe('complete');
  });

  it('the timer firing during a drain checks again until the drain ends, then sends', async () => {
    // The photo comes first, so the drain skips it (not due yet) and moves on
    // to five text searches, 2 s apart: the drain runs until about 8 s.
    mockLoadJobs.mockResolvedValue([
      queuedJob(1, { error: 'unavailable', attempts: 1, nextAttemptAt: Date.now() + 3_000 }),
      ...[1, 2, 3, 4, 5].map(textJob),
    ]);
    mockGet.mockResolvedValue(TEXT_OK);
    mockPost.mockResolvedValue(OK);
    await setup();
    await act(async () => {
      scanMocks.netListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(7_000);
    });
    // Due at 3 s, but the drain is still running.
    expect(mockGet).toHaveBeenCalledTimes(4);
    expect(mockPost).not.toHaveBeenCalled();
    await advance(3_000);
    expect(mockGet).toHaveBeenCalledTimes(5);
    expect(attempts()).toEqual(['2']);
  });

  it('a photo held back for a wait that ends mid-drain is sent in the same drain (Codex)', async () => {
    // [ordinary photo, text search, photo due in 1 s]: the ordinary photo is
    // held back while the other waits; the text search and the 2 s delay let
    // the waiting photo become due and succeed in the same pass. Nothing else
    // reliably starts another drain (the retry timer's busy re-arm only does
    // when it hasn't been cleared yet), so the drain itself goes round again.
    mockLoadJobs.mockResolvedValue([
      queuedJob(2),
      textJob(1),
      queuedJob(1, { error: 'unavailable', attempts: 1, nextAttemptAt: Date.now() + 1_000 }),
    ]);
    mockGet.mockResolvedValue(TEXT_OK);
    mockPost.mockResolvedValue(OK);
    const { result } = await setup();
    await act(async () => {
      scanMocks.netListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(10_000);
    });
    expect(attempts()).toEqual(['2', '1']);
    expect(result.current.scan.jobs.find((j) => j.id === 'q-2')?.status).toBe('complete');
  });

  it('does not double-send a pending job that is in flight (inFlightRef)', async () => {
    mockPost.mockRejectedValueOnce(UNAVAILABLE).mockResolvedValue(OK);
    const { result } = await setup();
    await startPhoto(result.current.scan, 'file:///a.jpg');
    await advance(29_000);
    // A capture whose connectivity check is still running: the job is
    // 'pending' (not 'searching') and only inFlightRef keeps a drain off it.
    let releaseNet: (v: unknown) => void = () => {};
    mockNetFetch.mockImplementationOnce(() => new Promise((r) => (releaseNet = r)));
    let capture: Promise<void> = Promise.resolve();
    await act(async () => {
      capture = Promise.resolve(result.current.scan.startScan('image', 'file:///b.jpg'));
    });
    expect(result.current.scan.jobs[0].status).toBe('pending');
    // The timer drains at 30 s: it sends a (due) and must skip b.
    await advance(10_000);
    expect(attempts()).toEqual(['1', '2']);
    await act(async () => {
      releaseNet({ isConnected: true });
      await capture;
      await jest.advanceTimersByTimeAsync(0);
    });
    await advance(60_000);
    expect(attempts()).toEqual(['1', '2', '1']);
    expect(result.current.scan.jobs.every((j) => j.status === 'complete')).toBe(true);
  });
});
