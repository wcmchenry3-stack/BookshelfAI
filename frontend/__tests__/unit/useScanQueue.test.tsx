import { act, renderHook } from '@testing-library/react-native';

import { useScanQueue, type ScanQueueDeps } from '../../hooks/useScanQueue';
import { SCAN_RETRY_DELAYS_MS } from '../../lib/scanErrors';
import type { ScanJob } from '../../lib/scanJob';

// #805: the queue drain, retry timer and send entry points, moved out of
// ScanJobProvider, tested on their own with the provider's refs as plain
// objects. Integration coverage stays in ScanJobContext / ScanRetrySchedule.

const mockNetFetch = jest.fn();
let mockNetListener: ((s: { isConnected: boolean }) => void) | null = null;
const mockUnsubscribe = jest.fn();
jest.mock('@react-native-community/netinfo', () => ({
  __esModule: true,
  default: {
    fetch: () => mockNetFetch(),
    addEventListener: (cb: (s: { isConnected: boolean }) => void) => {
      mockNetListener = cb;
      return mockUnsubscribe;
    },
  },
}));
jest.mock('../../lib/scanJobStorage', () => ({ deleteScanImage: jest.fn() }));
jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureException: jest.fn(), captureMessage: jest.fn() },
}));
let mockUuid = 0;
jest.mock('expo-crypto', () => ({ randomUUID: () => `uuid-${++mockUuid}` }));

const NOW = 1_000_000;

function job(id: string, over: Partial<ScanJob> = {}): ScanJob {
  return {
    id,
    type: 'image',
    status: 'queued',
    createdAt: 1,
    imageUri: `file:///${id}.jpg`,
    retryCount: 0,
    ...over,
  };
}

/** A deferred promise, to hold a send open while the test looks around. */
function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => (resolve = r));
  return { promise, resolve };
}

function makeDeps(initial: ScanJob[], over: Partial<ScanQueueDeps> = {}): ScanQueueDeps {
  const jobsRef = { current: initial };
  const setJobsAndRef = jest.fn((u: (prev: ScanJob[]) => ScanJob[]) => {
    jobsRef.current = u(jobsRef.current);
  });
  const updateJob = jest.fn((id: string, updates: Partial<ScanJob>) =>
    setJobsAndRef((list) => list.map((j) => (j.id === id ? { ...j, ...updates } : j)))
  );
  // By default a send completes the job.
  const send = jest.fn(async (j: ScanJob) => updateJob(j.id, { status: 'complete' }));
  return {
    jobs: initial,
    loaded: true,
    jobsRef,
    setJobsAndRef,
    updateJob,
    inFlightRef: { current: new Set<string>() },
    generationRef: { current: 0 },
    drainingRef: { current: false },
    drainAgainRef: { current: false },
    stopDrainRef: { current: 'none' },
    imageInFlight: jest.fn(() => false),
    setScanNotice: jest.fn(),
    showBanner: jest.fn(),
    t: (key: string) => key,
    executeScanRef: { current: send },
    ...over,
  };
}

function sent(deps: ScanQueueDeps): string[] {
  return (deps.executeScanRef.current as jest.Mock).mock.calls.map((c) => (c[0] as ScanJob).id);
}

beforeEach(() => {
  jest.useFakeTimers({ now: NOW });
  mockNetFetch.mockReset().mockResolvedValue({ isConnected: true });
  mockNetListener = null;
  mockUnsubscribe.mockReset();
});
afterEach(() => {
  jest.useRealTimers();
});

describe('drainQueue', () => {
  it('sends queued jobs in order, waiting between them, and clears drainingRef', async () => {
    const deps = makeDeps([job('a'), job('b')]);
    const { result } = await renderHook(() => useScanQueue(deps));
    let done = false;
    await act(async () => {
      void result.current.drainQueue().then(() => (done = true));
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(sent(deps)).toEqual(['a']);
    expect(deps.drainingRef.current).toBe(true);
    await act(async () => {
      await jest.advanceTimersByTimeAsync(2000);
    });
    expect(sent(deps)).toEqual(['a', 'b']);
    expect(done).toBe(true);
    expect(deps.drainingRef.current).toBe(false);
  });

  it('stops at a known-offline state and sends nothing', async () => {
    mockNetFetch.mockResolvedValue({ isConnected: false });
    const deps = makeDeps([job('a')]);
    const { result } = await renderHook(() => useScanQueue(deps));
    await act(async () => {
      await result.current.drainQueue();
    });
    expect(sent(deps)).toEqual([]);
    expect(deps.drainingRef.current).toBe(false);
  });

  it('runs again when drainAgainRef is set during the drain', async () => {
    const deps = makeDeps([job('a')]);
    const first = deps.executeScanRef.current;
    // While `a` is sent, another photo is handed to the drain (#605).
    deps.executeScanRef.current = jest.fn(async (j: ScanJob) => {
      await first(j);
      if (j.id === 'a') {
        deps.setJobsAndRef((l) => [...l, job('b')]);
        deps.drainAgainRef.current = true;
      }
    });
    const { result } = await renderHook(() => useScanQueue(deps));
    await act(async () => {
      await result.current.drainQueue();
    });
    expect(sent(deps)).toEqual(['a', 'b']);
    expect(deps.drainAgainRef.current).toBe(false);
  });

  it('skips jobs that are in flight and photos not yet due', async () => {
    const deps = makeDeps([
      job('busy'),
      job('later', { error: 'unavailable', attempts: 1, nextAttemptAt: NOW + 60_000 }),
      job('text', { type: 'text', imageUri: undefined, query: 'dune' }),
    ]);
    deps.inFlightRef.current.add('busy');
    const { result } = await renderHook(() => useScanQueue(deps));
    await act(async () => {
      await result.current.drainQueue();
    });
    expect(sent(deps)).toEqual(['text']);
  });

  it('stops sending photos after a refusal narrows stopDrainRef, but still sends text', async () => {
    const deps = makeDeps([
      job('a'),
      job('b'),
      job('t', { type: 'text', imageUri: undefined, query: 'q' }),
    ]);
    deps.executeScanRef.current = jest.fn(async (j: ScanJob) => {
      deps.updateJob(j.id, { status: 'queued', error: 'limit' });
      deps.stopDrainRef.current = 'images';
    });
    const { result } = await renderHook(() => useScanQueue(deps));
    await act(async () => {
      const p = result.current.drainQueue();
      await jest.advanceTimersByTimeAsync(10_000);
      await p;
    });
    expect(sent(deps)).toEqual(['a', 't']);
  });
});

describe('one drain at a time', () => {
  it('resumeQueue clears the notice and does not start a second drain while one runs', async () => {
    const hold = deferred();
    const deps = makeDeps([job('a')]);
    deps.executeScanRef.current = jest.fn(() => hold.promise);
    const { result } = await renderHook(() => useScanQueue(deps));
    await act(async () => {
      void result.current.drainQueue();
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(deps.drainingRef.current).toBe(true);
    await act(async () => {
      result.current.resumeQueue();
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(deps.setScanNotice).toHaveBeenCalledWith(null);
    expect(sent(deps)).toEqual(['a']);
    await act(async () => {
      hold.resolve();
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(deps.drainingRef.current).toBe(false);
  });

  it('a reconnect drains, but not while a drain is running', async () => {
    const hold = deferred();
    const deps = makeDeps([job('a')]);
    deps.executeScanRef.current = jest.fn(() => hold.promise);
    await renderHook(() => useScanQueue(deps));
    expect(mockNetListener).not.toBeNull();
    await act(async () => {
      mockNetListener!({ isConnected: true });
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(sent(deps)).toEqual(['a']);
    await act(async () => {
      mockNetListener!({ isConnected: true });
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(sent(deps)).toEqual(['a']);
    await act(async () => {
      hold.resolve();
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(deps.drainingRef.current).toBe(false);
  });

  it('restartScanSchedule hands a photo to the running drain via drainAgainRef', async () => {
    const deps = makeDeps([job('f', { status: 'failed', attempts: 3, error: 'unavailable' })]);
    deps.drainingRef.current = true;
    const { result } = await renderHook(() => useScanQueue(deps));
    await act(async () => {
      await result.current.restartScanSchedule('f');
    });
    expect(sent(deps)).toEqual([]);
    expect(deps.drainAgainRef.current).toBe(true);
    expect(deps.jobsRef.current[0]).toMatchObject({
      status: 'queued',
      retryCount: 0,
      attempts: undefined,
      nextAttemptAt: undefined,
    });
  });

  it('restartScanSchedule sends the photo now when nothing else is sending', async () => {
    const deps = makeDeps([job('f', { status: 'failed', attempts: 3, error: 'unavailable' })]);
    const { result } = await renderHook(() => useScanQueue(deps));
    await act(async () => {
      await result.current.restartScanSchedule('f');
    });
    expect(sent(deps)).toEqual(['f']);
    expect((deps.executeScanRef.current as jest.Mock).mock.calls[0][0]).toMatchObject({
      status: 'pending',
      retryCount: 0,
      attempts: undefined,
    });
  });
});

describe('retry timer', () => {
  const waiting = () =>
    job('w', {
      error: 'unavailable',
      attempts: 1,
      nextAttemptAt: NOW + SCAN_RETRY_DELAYS_MS[0],
    });

  it('drains when the earliest scheduled attempt is due, not before', async () => {
    const deps = makeDeps([waiting()]);
    await renderHook(() => useScanQueue(deps));
    await act(async () => {
      await jest.advanceTimersByTimeAsync(SCAN_RETRY_DELAYS_MS[0] - 1);
    });
    expect(sent(deps)).toEqual([]);
    await act(async () => {
      await jest.advanceTimersByTimeAsync(1);
    });
    expect(sent(deps)).toEqual(['w']);
  });

  it('is not armed before the queue has loaded', async () => {
    const deps = makeDeps([waiting()], { loaded: false });
    await renderHook(() => useScanQueue(deps));
    await act(async () => {
      await jest.advanceTimersByTimeAsync(SCAN_RETRY_DELAYS_MS[0] * 2);
    });
    expect(sent(deps)).toEqual([]);
  });

  it('checks again after a minute when offline at the due time', async () => {
    mockNetFetch.mockResolvedValue({ isConnected: false });
    const deps = makeDeps([waiting()]);
    await renderHook(() => useScanQueue(deps));
    await act(async () => {
      await jest.advanceTimersByTimeAsync(SCAN_RETRY_DELAYS_MS[0]);
    });
    expect(mockNetFetch).toHaveBeenCalledTimes(1);
    mockNetFetch.mockResolvedValue({ isConnected: true });
    await act(async () => {
      await jest.advanceTimersByTimeAsync(60_000 - 1);
    });
    expect(sent(deps)).toEqual([]);
    await act(async () => {
      await jest.advanceTimersByTimeAsync(1);
    });
    expect(sent(deps)).toEqual(['w']);
  });

  it('waits a second and looks again when a drain is running at the due time', async () => {
    const deps = makeDeps([waiting()]);
    deps.drainingRef.current = true;
    await renderHook(() => useScanQueue(deps));
    await act(async () => {
      await jest.advanceTimersByTimeAsync(SCAN_RETRY_DELAYS_MS[0]);
    });
    expect(sent(deps)).toEqual([]);
    expect(mockNetFetch).not.toHaveBeenCalled();
    deps.drainingRef.current = false;
    await act(async () => {
      await jest.advanceTimersByTimeAsync(1000);
    });
    expect(sent(deps)).toEqual(['w']);
  });

  it('is cancelled when no photo waits any more', async () => {
    const deps = makeDeps([waiting()]);
    const hook = await renderHook((props: ScanQueueDeps) => useScanQueue(props), {
      initialProps: deps,
    });
    const settled = [job('w', { status: 'complete' })];
    deps.jobsRef.current = settled;
    await hook.rerender({ ...deps, jobs: settled });
    await act(async () => {
      await jest.advanceTimersByTimeAsync(SCAN_RETRY_DELAYS_MS[0] * 2);
    });
    // The old timer never fired: it would have checked connectivity first.
    expect(mockNetFetch).not.toHaveBeenCalled();
    expect(sent(deps)).toEqual([]);
  });

  it('is cleared, and the listener removed, on unmount', async () => {
    const deps = makeDeps([waiting()]);
    const hook = await renderHook(() => useScanQueue(deps));
    await hook.unmount();
    expect(mockUnsubscribe).toHaveBeenCalled();
    await act(async () => {
      await jest.advanceTimersByTimeAsync(SCAN_RETRY_DELAYS_MS[0] * 2);
    });
    // The timer never fired: it would have checked connectivity first.
    expect(mockNetFetch).not.toHaveBeenCalled();
    expect(sent(deps)).toEqual([]);
  });
});

describe('send entry points', () => {
  it('startScan drops the job without sending when signed out during the connectivity check', async () => {
    const deps = makeDeps([]);
    mockNetFetch.mockImplementation(async () => {
      deps.generationRef.current += 1;
      return { isConnected: true };
    });
    const { result } = await renderHook(() => useScanQueue(deps));
    await act(async () => {
      await result.current.startScan('image', 'file:///new.jpg');
    });
    expect(sent(deps)).toEqual([]);
    expect(deps.showBanner).not.toHaveBeenCalled();
    expect(deps.inFlightRef.current.size).toBe(0);
  });

  it('startScan queues the job offline with the savedOffline banner', async () => {
    mockNetFetch.mockResolvedValue({ isConnected: false });
    const deps = makeDeps([]);
    const { result } = await renderHook(() => useScanQueue(deps));
    await act(async () => {
      await result.current.startScan('text', undefined, 'dune');
    });
    expect(deps.jobsRef.current[0]).toMatchObject({ type: 'text', status: 'queued' });
    expect(deps.showBanner).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'savedOffline' })
    );
    expect(sent(deps)).toEqual([]);
  });

  it('retryScan resends with one more retry, and queues the job after MAX_RETRIES', async () => {
    const deps = makeDeps([
      job('a', { status: 'failed', retryCount: 0 }),
      job('b', { status: 'failed', retryCount: 3 }),
    ]);
    const { result } = await renderHook(() => useScanQueue(deps));
    await act(async () => {
      await result.current.retryScan('a');
      await result.current.retryScan('b');
    });
    expect(sent(deps)).toEqual(['a']);
    expect((deps.executeScanRef.current as jest.Mock).mock.calls[0][0]).toMatchObject({
      retryCount: 1,
      status: 'pending',
    });
    expect(deps.updateJob).toHaveBeenCalledWith('b', { status: 'queued' });
    expect(deps.showBanner).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'maxRetries' })
    );
  });

  it('keeps the same callbacks across renders (no new provider renders)', async () => {
    const deps = makeDeps([]);
    const hook = await renderHook(() => useScanQueue(deps));
    const before = hook.result.current;
    await hook.rerender({});
    const after = hook.result.current;
    expect(after.drainQueue).toBe(before.drainQueue);
    expect(after.resumeQueue).toBe(before.resumeQueue);
    expect(after.startScan).toBe(before.startScan);
    expect(after.retryScan).toBe(before.retryScan);
    expect(after.restartScanSchedule).toBe(before.restartScanSchedule);
  });
});
