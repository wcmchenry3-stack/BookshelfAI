/**
 * executeScan and AI consent (#1023): no photo is uploaded before consent
 * stands, a user's own send may ask, the drain never asks, and a server
 * `403 ai_consent_required` asks again instead of failing the photo.
 */
import { api } from '../../lib/api';
import { isReportedCentrally } from '../../lib/apiErrorReporting';
import { executeScan } from '../../lib/scan/executeScan';
import { shouldBumpUsageOnError } from '../../lib/scan/scanOutcomes';
import type { ScanAiConsentGate, ScanExecDeps } from '../../lib/scan/types';
import { AI_CONSENT_ERROR, classifyScanError, hasRefusedJobs } from '../../lib/scanErrors';
import type { ScanJob } from '../../lib/scanJob';
import { Sentry } from '../../lib/sentry';

jest.mock('../../lib/api', () => ({ api: { get: jest.fn(), post: jest.fn() } }));
jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureMessage: jest.fn(), captureException: jest.fn() },
}));

const post = api.post as jest.Mock;
const get = api.get as jest.Mock;
const REQUIRED = { response: { status: 403, data: { detail: 'ai_consent_required' } } };

function makeJob(over: Partial<ScanJob> = {}): ScanJob {
  return {
    id: 'job-1',
    type: 'image',
    imageUri: 'file:///x.jpg',
    status: 'pending',
    createdAt: 1,
    retryCount: 0,
    ...over,
  } as ScanJob;
}

function makeGate(answer: boolean): jest.Mocked<ScanAiConsentGate> {
  return {
    check: jest.fn(async () => answer),
    ensure: jest.fn(async () => answer),
    markRequired: jest.fn(),
  };
}

function makeDeps(gate: ScanAiConsentGate, over: Partial<ScanExecDeps> = {}): ScanExecDeps {
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
    aiConsentRef: { current: gate },
    ...over,
  };
}

const HELD = {
  status: 'queued',
  error: AI_CONSENT_ERROR,
  retryCount: 0,
  attempts: undefined,
  nextAttemptAt: undefined,
};

beforeEach(() => {
  jest.clearAllMocks();
  post.mockResolvedValue({ data: { books: [{ title: 'Dune', author: 'Herbert' }] } });
  get.mockResolvedValue({ data: [{ title: 'Dune', author: 'Herbert' }] });
});

describe('the upload gate', () => {
  it('the drain never asks: without consent the photo waits, nothing is sent', async () => {
    const gate = makeGate(false);
    const deps = makeDeps(gate);
    await executeScan(deps, makeJob({ status: 'queued' }));
    expect(gate.check).toHaveBeenCalledTimes(1);
    expect(gate.ensure).not.toHaveBeenCalled();
    expect(post).not.toHaveBeenCalled();
    expect(deps.updateJob).toHaveBeenCalledWith('job-1', HELD);
    expect(deps.updateJob).not.toHaveBeenCalledWith('job-1', { status: 'searching' });
    // The rest of this drain's photos wait too.
    expect(deps.stopDrainRef.current).toBe('images');
    expect(deps.inFlightRef.current.size).toBe(0);
    expect(deps.showBanner).not.toHaveBeenCalled();
    expect(Sentry.captureException).not.toHaveBeenCalled();
  });

  it("a user's own send asks; Not now sends nothing and the photo waits", async () => {
    const gate = makeGate(false);
    const deps = makeDeps(gate);
    await executeScan(deps, makeJob(), { interactive: true });
    expect(gate.ensure).toHaveBeenCalledTimes(1);
    expect(gate.check).not.toHaveBeenCalled();
    expect(post).not.toHaveBeenCalled();
    expect(deps.updateJob).toHaveBeenCalledWith('job-1', HELD);
  });

  it('Continue (consent recorded) lets the photo go', async () => {
    const gate = makeGate(true);
    const deps = makeDeps(gate);
    await executeScan(deps, makeJob(), { interactive: true });
    expect(post).toHaveBeenCalledTimes(1);
    expect(post.mock.calls[0][0]).toBe('/scan');
    // The gate answered before the upload started.
    expect(gate.ensure.mock.invocationCallOrder[0]).toBeLessThan(post.mock.invocationCallOrder[0]);
  });

  it('with consent the drain sends the photo', async () => {
    const deps = makeDeps(makeGate(true));
    await executeScan(deps, makeJob({ status: 'queued' }));
    expect(post).toHaveBeenCalledTimes(1);
  });

  it('a text search sends no photo and is never gated', async () => {
    const gate = makeGate(false);
    const deps = makeDeps(gate);
    await executeScan(deps, makeJob({ type: 'text', imageUri: undefined, query: 'dune' }), {
      interactive: true,
    });
    expect(gate.check).not.toHaveBeenCalled();
    expect(gate.ensure).not.toHaveBeenCalled();
    expect(get).toHaveBeenCalledWith('/books/search', expect.anything());
  });

  it('an enhanced re-scan without consent gives the standard books back', async () => {
    const deps = makeDeps(makeGate(false));
    const job = makeJob({
      enhanced: true,
      results: [{ title: 'Dune', author: 'Herbert' }] as ScanJob['results'],
    });
    await executeScan(deps, job, { interactive: true });
    expect(post).not.toHaveBeenCalled();
    expect(deps.updateJob).toHaveBeenCalledWith('job-1', {
      status: 'complete',
      error: undefined,
      enhanced: false,
    });
    expect(deps.setReviewingJobId).toHaveBeenCalledWith('job-1');
  });

  it('signed out while asking: nothing is held or sent', async () => {
    const generationRef = { current: 0 };
    const gate: ScanAiConsentGate = {
      check: jest.fn(),
      ensure: jest.fn(async () => {
        generationRef.current += 1;
        return true;
      }),
      markRequired: jest.fn(),
    };
    const deps = makeDeps(gate, { generationRef });
    await executeScan(deps, makeJob(), { interactive: true });
    expect(post).not.toHaveBeenCalled();
    expect(deps.updateJob).not.toHaveBeenCalled();
  });
});

describe('403 ai_consent_required from the server', () => {
  it('is its own state, not counted, not reported centrally', () => {
    expect(classifyScanError(REQUIRED)).toEqual({ kind: 'ai_consent_required' });
    expect(shouldBumpUsageOnError(makeJob(), REQUIRED, { kind: 'ai_consent_required' })).toBe(
      false
    );
    expect(isReportedCentrally(REQUIRED)).toBe(false);
  });

  it('asks again instead of an error, and the photo waits', async () => {
    const gate = makeGate(true);
    post.mockRejectedValue(REQUIRED);
    const deps = makeDeps(gate);
    await executeScan(deps, makeJob({ status: 'queued' }));
    expect(gate.markRequired).toHaveBeenCalledTimes(1);
    expect(gate.ensure).toHaveBeenCalledTimes(1);
    expect(deps.updateJob).toHaveBeenLastCalledWith('job-1', HELD);
    expect(deps.updateJob).not.toHaveBeenCalledWith(
      'job-1',
      expect.objectContaining({ status: 'failed' })
    );
    expect(deps.showBanner).not.toHaveBeenCalled();
    expect(deps.bumpUsage).not.toHaveBeenCalled();
    expect(Sentry.captureException).not.toHaveBeenCalled();
  });

  it('a held photo counts as refused (the queue-full copy says it will go later)', () => {
    expect(hasRefusedJobs([{ status: 'queued', error: AI_CONSENT_ERROR }])).toBe(true);
  });
});
