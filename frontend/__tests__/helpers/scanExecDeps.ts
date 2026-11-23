/**
 * Plain `executeScan` inputs for unit tests that drive it directly: a photo
 * job and a `ScanExecDeps` with every callback a jest.fn() and AI consent
 * given. Pass overrides for what the test is about.
 */
import type { ScanExecDeps } from '../../lib/scan/types';
import type { ScanJob } from '../../lib/scanJob';

export function makeImageJob(over: Partial<ScanJob> = {}): ScanJob {
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

export function makeScanExecDeps(over: Partial<ScanExecDeps> = {}): ScanExecDeps {
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
      current: {
        check: jest.fn(async () => true),
        ensure: jest.fn(async () => true),
        markRequired: jest.fn(),
      },
    },
    ...over,
  };
}
