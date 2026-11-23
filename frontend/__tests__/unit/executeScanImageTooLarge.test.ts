/**
 * executeScan and a 413 (#668): the photo is over the server's limits, so the
 * same photo would fail the same way. It is failed for good (no retry
 * schedule, no Retry action), with its own copy, and counts nothing.
 */
import { api } from '../../lib/api';
import { executeScan } from '../../lib/scan/executeScan';
import { shouldBumpUsageOnError } from '../../lib/scan/scanOutcomes';
import type { ScanExecDeps } from '../../lib/scan/types';
import { IMAGE_TOO_LARGE_ERROR } from '../../lib/scanErrors';
import type { ScanJob } from '../../lib/scanJob';
import { Sentry } from '../../lib/sentry';

jest.mock('../../lib/api', () => ({ api: { get: jest.fn(), post: jest.fn() } }));
jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureMessage: jest.fn(), captureException: jest.fn() },
}));

const post = api.post as jest.Mock;
const TOO_LARGE = { response: { status: 413, data: { detail: 'image_too_large_pixels' } } };

function makeJob(over: Partial<ScanJob> = {}): ScanJob {
  return {
    id: 'job-1',
    type: 'image',
    imageUri: 'file:///x.jpg',
    status: 'pending',
    createdAt: 1,
    ...over,
  } as ScanJob;
}

function makeDeps(): ScanExecDeps {
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
    aiConsentRef: {
      current: { check: async () => true, ensure: async () => true, markRequired: jest.fn() },
    },
  } as unknown as ScanExecDeps;
}

beforeEach(() => {
  jest.clearAllMocks();
  post.mockRejectedValue(TOO_LARGE);
});

describe('413 from POST /scan', () => {
  it('fails the photo for good with its own banner, no Retry action, one attempt', async () => {
    const deps = makeDeps();
    await executeScan(deps, makeJob(), { interactive: true });
    expect(post).toHaveBeenCalledTimes(1);
    expect(deps.updateJob).toHaveBeenLastCalledWith('job-1', {
      status: 'failed',
      error: IMAGE_TOO_LARGE_ERROR,
      attempts: undefined,
      nextAttemptAt: undefined,
    });
    expect(deps.showBanner).toHaveBeenCalledTimes(1);
    const banner = (deps.showBanner as jest.Mock).mock.calls[0][0];
    expect(banner.message).toBe('imageTooLarge');
    expect(banner.actions).toBeUndefined();
    expect(deps.bumpUsage).not.toHaveBeenCalled();
    expect(Sentry.captureException).not.toHaveBeenCalled();
  });

  it('is not retried by the queue drain either', async () => {
    const deps = makeDeps();
    await executeScan(deps, makeJob({ status: 'queued', attempts: 2 }));
    expect(deps.updateJob).toHaveBeenLastCalledWith(
      'job-1',
      expect.objectContaining({ status: 'failed', nextAttemptAt: undefined, attempts: undefined })
    );
  });

  it('keeps the standard books when an enhanced re-scan is refused as too large', async () => {
    const deps = makeDeps();
    const results = [{ title: 'A' }] as unknown as ScanJob['results'];
    await executeScan(deps, makeJob({ enhanced: true, results }), { interactive: true });
    expect(deps.updateJob).toHaveBeenLastCalledWith(
      'job-1',
      expect.objectContaining({ status: 'complete', enhanced: false })
    );
    expect(deps.setReviewingJobId).toHaveBeenCalledWith('job-1');
  });

  it('does not move the usage counters', () => {
    expect(shouldBumpUsageOnError(makeJob(), TOO_LARGE, { kind: 'image_too_large' })).toBe(false);
  });
});
