import { api } from '../../lib/api';
import { executeScan } from '../../lib/scan/executeScan';
import {
  classifyJobFailure,
  handleEnhancedRefusal,
  handleScanRefusal,
  handleUnavailable,
  isNetworkSuspend,
  restorePriorResults,
  shouldBumpUsageOnError,
} from '../../lib/scan/scanOutcomes';
import type { ScanExecDeps } from '../../lib/scan/types';
import type { ScanUiState } from '../../lib/scanErrors';
import { MAX_SCAN_ATTEMPTS, SCAN_RETRY_DELAYS_MS } from '../../lib/scanErrors';
import type { ScanJob } from '../../lib/scanJob';

jest.mock('../../lib/api', () => ({ api: { get: jest.fn(), post: jest.fn() } }));
jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureMessage: jest.fn(), captureException: jest.fn() },
}));

function makeJob(over: Partial<ScanJob> = {}): ScanJob {
  return {
    id: 'job-1',
    type: 'image',
    imageUri: 'file:///x.jpg',
    status: 'searching',
    createdAt: 1,
    retryCount: 0,
    ...over,
  } as ScanJob;
}

function makeDeps(over: Partial<ScanExecDeps> = {}): ScanExecDeps {
  return {
    updateJob: jest.fn(),
    setJobsAndRef: jest.fn(),
    setReviewingJobId: jest.fn(),
    setScanNotice: jest.fn(),
    setEnhancedScansRemaining: jest.fn(),
    showBanner: jest.fn(),
    bumpUsage: jest.fn(),
    drainQueue: jest.fn(async () => {}),
    imageInFlight: jest.fn(() => false),
    retryScan: jest.fn(),
    queueForLater: jest.fn(),
    requestEnhancedScan: jest.fn(),
    restartScanSchedule: jest.fn(async () => {}),
    enhancedScansRemaining: null,
    t: (key: string) => key,
    i18n: { language: 'en' },
    jobsRef: { current: [] },
    inFlightRef: { current: new Set<string>() },
    abortersRef: { current: new Set<AbortController>() },
    generationRef: { current: 0 },
    drainingRef: { current: false },
    drainAgainRef: { current: false },
    stopDrainRef: { current: 'none' },
    getTurnstileTokenRef: { current: async () => null },
    turnstileConfiguredRef: { current: true },
    ...over,
  };
}

describe('restorePriorResults', () => {
  it('completes the job, reopens the review and shows the message', () => {
    const deps = makeDeps();
    restorePriorResults(deps, makeJob(), { enhanced: false }, 'hello', 'info');
    expect(deps.updateJob).toHaveBeenCalledWith('job-1', {
      status: 'complete',
      error: undefined,
      enhanced: false,
    });
    expect(deps.setReviewingJobId).toHaveBeenCalledWith('job-1');
    expect(deps.showBanner).toHaveBeenCalledWith({
      message: 'hello',
      type: 'info',
      duration: 6000,
    });
  });

  it('shows no banner when the message is null', () => {
    const deps = makeDeps();
    restorePriorResults(deps, makeJob(), {}, null, 'info');
    expect(deps.showBanner).not.toHaveBeenCalled();
    expect(deps.setReviewingJobId).toHaveBeenCalledWith('job-1');
  });
});

describe('handleScanRefusal', () => {
  it('limit: sets the notice, requeues the job and stops all photos', () => {
    const deps = makeDeps();
    handleScanRefusal(deps, makeJob(), {
      kind: 'daily_limit',
      retryAfterSeconds: 60,
    } as ScanUiState);
    expect(deps.setScanNotice).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'daily_limit' })
    );
    expect(deps.updateJob).toHaveBeenCalledWith('job-1', {
      status: 'queued',
      error: 'daily_limit',
      retryCount: 0,
      attempts: undefined,
      nextAttemptAt: undefined,
    });
    expect(deps.stopDrainRef.current).toBe('images');
    expect(deps.showBanner).not.toHaveBeenCalled();
  });

  it('busy: requeues with the in_progress error', () => {
    const deps = makeDeps();
    handleScanRefusal(deps, makeJob(), { kind: 'in_progress', retryAfterSeconds: 3 });
    expect(deps.updateJob).toHaveBeenCalledWith(
      'job-1',
      expect.objectContaining({ status: 'queued', error: 'in_progress' })
    );
    expect(deps.stopDrainRef.current).toBe('images');
  });

  it('paused: requeues and stops photos; enhanced-only pause stops enhanced photos only', () => {
    const deps = makeDeps();
    handleScanRefusal(deps, makeJob(), { kind: 'paused' });
    expect(deps.stopDrainRef.current).toBe('images');
    expect(deps.setScanNotice).toHaveBeenCalledWith(expect.objectContaining({ kind: 'paused' }));

    const enhancedOnly = makeDeps();
    handleScanRefusal(enhancedOnly, makeJob(), { kind: 'paused', enhancedOnly: true });
    expect(enhancedOnly.stopDrainRef.current).toBe('enhanced');
  });

  it('never narrows a stop that already covers all photos', () => {
    const deps = makeDeps({ stopDrainRef: { current: 'images' } });
    handleScanRefusal(deps, makeJob(), { kind: 'paused', enhancedOnly: true });
    expect(deps.stopDrainRef.current).toBe('images');
  });

  it('a refused enhanced re-scan with earlier results restores them, with the notice and no banner', () => {
    const deps = makeDeps();
    const job = makeJob({ enhanced: true, results: [{ title: 'A', author: 'B' }] as never });
    handleScanRefusal(deps, job, { kind: 'paused' });
    expect(deps.setScanNotice).toHaveBeenCalled();
    expect(deps.updateJob).toHaveBeenCalledWith('job-1', {
      status: 'complete',
      error: undefined,
      enhanced: false,
    });
    expect(deps.setReviewingJobId).toHaveBeenCalledWith('job-1');
    expect(deps.showBanner).not.toHaveBeenCalled();
  });

  it('does nothing for a state without a notice', () => {
    const deps = makeDeps();
    handleScanRefusal(deps, makeJob(), { kind: 'generic' });
    expect(deps.updateJob).not.toHaveBeenCalled();
    expect(deps.setScanNotice).not.toHaveBeenCalled();
  });
});

describe('handleUnavailable', () => {
  const state = { kind: 'unavailable', code: 'scan_unavailable' } as Extract<
    ScanUiState,
    { kind: 'unavailable' }
  >;

  it('first attempt: waits on the schedule, shows the short notice and holds the photo queue', () => {
    jest.spyOn(Date, 'now').mockReturnValue(1_000_000);
    const deps = makeDeps();
    handleUnavailable(deps, makeJob(), 1, state);
    expect(deps.stopDrainRef.current).toBe('images');
    expect(deps.updateJob).toHaveBeenCalledWith('job-1', {
      status: 'queued',
      error: 'unavailable',
      retryCount: 0,
      attempts: 1,
      nextAttemptAt: 1_000_000 + SCAN_RETRY_DELAYS_MS[0],
    });
    expect(deps.setScanNotice).toHaveBeenCalledWith(expect.objectContaining({ kind: 'paused' }));
    jest.restoreAllMocks();
  });

  it('later attempt: schedules the next step, no new notice, clears the paused one', () => {
    jest.spyOn(Date, 'now').mockReturnValue(2_000_000);
    const deps = makeDeps();
    handleUnavailable(deps, makeJob(), 2, state);
    expect(deps.updateJob).toHaveBeenCalledWith('job-1', {
      status: 'queued',
      error: 'unavailable',
      retryCount: 0,
      attempts: 2,
      nextAttemptAt: 2_000_000 + SCAN_RETRY_DELAYS_MS[1],
    });
    const call = (deps.setScanNotice as jest.Mock).mock.calls[0][0];
    expect(typeof call).toBe('function');
    expect(call({ kind: 'paused' })).toBeNull();
    expect(call({ kind: 'daily_limit' })).toEqual({ kind: 'daily_limit' });
    jest.restoreAllMocks();
  });

  it('final attempt: fails the job, shows the final banner and starts a drain for waiting photos', () => {
    const waiting = makeJob({ id: 'job-2', status: 'queued' });
    const deps = makeDeps({ jobsRef: { current: [waiting] } });
    handleUnavailable(deps, makeJob(), MAX_SCAN_ATTEMPTS, state);
    expect(deps.updateJob).toHaveBeenCalledWith('job-1', {
      status: 'failed',
      error: 'unavailable',
      attempts: MAX_SCAN_ATTEMPTS,
      nextAttemptAt: undefined,
    });
    expect(deps.showBanner).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'scanFailedFinal', type: 'error', duration: 8000 })
    );
    expect(deps.drainQueue).toHaveBeenCalledTimes(1);
  });

  it('final attempt: does not start a drain while one is running', () => {
    const waiting = makeJob({ id: 'job-2', status: 'queued' });
    const deps = makeDeps({
      jobsRef: { current: [waiting] },
      drainingRef: { current: true },
    });
    handleUnavailable(deps, makeJob(), MAX_SCAN_ATTEMPTS, state);
    expect(deps.drainQueue).not.toHaveBeenCalled();
  });
});

describe('handleEnhancedRefusal', () => {
  const state = { kind: 'enhanced_refused', reason: 'premium_required' } as Extract<
    ScanUiState,
    { kind: 'enhanced_refused' }
  >;

  it('with earlier results: restores them and hides the offer', () => {
    const deps = makeDeps();
    const job = makeJob({ enhanced: true, results: [{ title: 'A', author: 'B' }] as never });
    handleEnhancedRefusal(deps, job, state);
    expect(deps.setEnhancedScansRemaining).toHaveBeenCalledWith(0);
    expect(deps.stopDrainRef.current).toBe('enhanced');
    expect(deps.setReviewingJobId).toHaveBeenCalledWith('job-1');
    expect(deps.updateJob).toHaveBeenCalledWith(
      'job-1',
      expect.objectContaining({ status: 'complete', enhanced: false })
    );
  });

  it('without results: fails the job with no_results and a banner', () => {
    const deps = makeDeps();
    handleEnhancedRefusal(deps, makeJob({ enhanced: true }), state);
    expect(deps.updateJob).toHaveBeenCalledWith('job-1', {
      status: 'failed',
      enhanced: false,
      error: 'no_results',
    });
    expect(deps.showBanner).toHaveBeenCalledWith(expect.objectContaining({ type: 'info' }));
  });

  it('keeps a wider stop already set', () => {
    const deps = makeDeps({ stopDrainRef: { current: 'images' } });
    handleEnhancedRefusal(deps, makeJob({ enhanced: true }), state);
    expect(deps.stopDrainRef.current).toBe('images');
  });
});

describe('classification helpers', () => {
  it('isNetworkSuspend is true only for an error with no response', () => {
    expect(isNetworkSuspend({ response: undefined })).toBe(true);
    expect(isNetworkSuspend({ response: null })).toBe(true);
    expect(isNetworkSuspend({ response: { status: 500 } })).toBe(false);
    expect(isNetworkSuspend(new Error('x'))).toBe(false);
    expect(isNetworkSuspend(null)).toBe(false);
    expect(isNetworkSuspend('x')).toBe(false);
  });

  it('classifyJobFailure turns turnstile_missing into generic only when Turnstile is not configured', () => {
    const missing = { kind: 'unavailable', code: 'turnstile_missing' } as ScanUiState;
    expect(classifyJobFailure(missing, false)).toEqual({ kind: 'generic' });
    expect(classifyJobFailure(missing, true)).toBe(missing);
    const failed = { kind: 'unavailable', code: 'turnstile_failed' } as ScanUiState;
    expect(classifyJobFailure(failed, false)).toBe(failed);
    const paused: ScanUiState = { kind: 'paused' };
    expect(classifyJobFailure(paused, false)).toBe(paused);
  });

  it('shouldBumpUsageOnError follows the counters rules', () => {
    const withResponse = { response: { status: 500 } };
    const image = makeJob();
    expect(shouldBumpUsageOnError(image, withResponse, { kind: 'generic' })).toBe(true);
    expect(
      shouldBumpUsageOnError(image, withResponse, { kind: 'daily_limit' } as ScanUiState)
    ).toBe(true);
    expect(shouldBumpUsageOnError(image, { response: undefined }, { kind: 'generic' })).toBe(false);
    expect(
      shouldBumpUsageOnError(makeJob({ type: 'text' }), withResponse, { kind: 'generic' })
    ).toBe(false);
    expect(shouldBumpUsageOnError(image, withResponse, { kind: 'paused' })).toBe(false);
    expect(
      shouldBumpUsageOnError(image, withResponse, {
        kind: 'in_progress',
        retryAfterSeconds: 1,
      })
    ).toBe(false);
    expect(
      shouldBumpUsageOnError(image, withResponse, {
        kind: 'unavailable',
        code: 'scan_unavailable',
      } as ScanUiState)
    ).toBe(false);
    expect(
      shouldBumpUsageOnError(image, withResponse, {
        kind: 'unavailable',
        code: 'turnstile_failed',
      } as ScanUiState)
    ).toBe(true);
    expect(
      shouldBumpUsageOnError(image, withResponse, { kind: 'requests_daily_limit' } as ScanUiState)
    ).toBe(false);
    expect(
      shouldBumpUsageOnError(image, withResponse, {
        kind: 'enhanced_refused',
        reason: 'premium_required',
      } as ScanUiState)
    ).toBe(false);
  });
});

describe('executeScan', () => {
  const post = api.post as jest.Mock;
  const inProgress = (retryAfter: string) => ({
    response: {
      status: 429,
      data: { detail: 'scan_in_progress' },
      headers: { 'retry-after': retryAfter },
    },
  });
  const okResponse = { data: { books: [{ title: 'T', author: 'A' }], no_match: null } };

  beforeEach(() => {
    post.mockReset();
    jest.useFakeTimers();
  });
  afterEach(() => {
    jest.useRealTimers();
  });

  it('requests a fresh Turnstile token for every POST, including scan_in_progress retries', async () => {
    let n = 0;
    const getToken = jest.fn(async () => `tok-${++n}`);
    const deps = makeDeps({ getTurnstileTokenRef: { current: getToken } });
    post
      .mockRejectedValueOnce(inProgress('2'))
      .mockRejectedValueOnce(inProgress('2'))
      .mockResolvedValueOnce(okResponse);
    const p = executeScan(deps, makeJob());
    await jest.advanceTimersByTimeAsync(10_000);
    await p;
    expect(post).toHaveBeenCalledTimes(3);
    expect(getToken).toHaveBeenCalledTimes(post.mock.calls.length);
    const sent = post.mock.calls.map((c) => (c[1] as FormData).get('cf-turnstile-response'));
    expect(sent).toEqual(['tok-1', 'tok-2', 'tok-3']);
  });

  it('silently retries scan_in_progress at most 3 times, waiting Retry-After each time', async () => {
    const deps = makeDeps();
    post.mockRejectedValue(inProgress('4'));
    const p = executeScan(deps, makeJob());
    await jest.advanceTimersByTimeAsync(3999);
    expect(post).toHaveBeenCalledTimes(1);
    await jest.advanceTimersByTimeAsync(1);
    expect(post).toHaveBeenCalledTimes(2);
    await jest.advanceTimersByTimeAsync(4000);
    expect(post).toHaveBeenCalledTimes(3);
    await jest.advanceTimersByTimeAsync(4000);
    expect(post).toHaveBeenCalledTimes(4);
    await jest.advanceTimersByTimeAsync(60_000);
    await p;
    // 1 first try + 3 retries, then the refusal path takes over.
    expect(post).toHaveBeenCalledTimes(4);
    expect(deps.updateJob).toHaveBeenCalledWith(
      'job-1',
      expect.objectContaining({ status: 'queued', error: 'in_progress' })
    );
  });

  it('runs a drain that was queued behind the send once it finishes', async () => {
    const deps = makeDeps();
    deps.drainAgainRef.current = true;
    post.mockResolvedValueOnce(okResponse);
    const p = executeScan(deps, makeJob());
    await jest.advanceTimersByTimeAsync(0);
    await p;
    expect(deps.drainAgainRef.current).toBe(false);
    expect(deps.drainQueue).toHaveBeenCalledTimes(1);
    expect(deps.inFlightRef.current.size).toBe(0);
    expect(deps.abortersRef.current.size).toBe(0);
  });

  it('does not drain again while a drain is running or a photo is still being sent', async () => {
    const draining = makeDeps({ drainingRef: { current: true } });
    draining.drainAgainRef.current = true;
    post.mockResolvedValue(okResponse);
    await executeScan(draining, makeJob());
    expect(draining.drainQueue).not.toHaveBeenCalled();
    expect(draining.drainAgainRef.current).toBe(true);

    const busy = makeDeps({ imageInFlight: jest.fn(() => true) });
    busy.drainAgainRef.current = true;
    await executeScan(busy, makeJob());
    expect(busy.drainQueue).not.toHaveBeenCalled();
  });
});
