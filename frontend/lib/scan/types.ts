import type { MutableRefObject } from 'react';

import type { BannerConfig } from '../../contexts/BannerContext';
import type { ScanNotice } from '../scanErrors';
import type { ScanJob } from '../scanJob';

type TFn = (key: string, options?: Record<string, unknown>) => string;

/** How a send was started: by the user (may ask for AI consent) or by the drain. */
export interface ExecuteScanOptions {
  /** True when the user just asked for this send, so the consent prompt may show. */
  interactive?: boolean;
}

/** AI consent as the scan pipeline sees it (#1023; AiConsentProvider's state). */
export interface ScanAiConsentGate {
  /** True only when consent is known to stand; never asks. */
  check: () => Promise<boolean>;
  /** Asks with the prompt when needed; true once consent stands. */
  ensure: () => Promise<boolean>;
  /** The server refused for lack of consent. */
  markRequired: () => void;
}

/**
 * Everything executeScan needs from ScanJobProvider, passed as one object.
 * Refs are the provider's own and are read at call time, never captured once;
 * the callbacks and `enhancedScansRemaining` are those of the render the
 * provider last rebuilt this object in.
 */
export interface ScanExecDeps {
  updateJob: (jobId: string, updates: Partial<ScanJob>) => void;
  setJobsAndRef: (updater: (prev: ScanJob[]) => ScanJob[]) => void;
  setReviewingJobId: (jobId: string | null) => void;
  setScanNotice: (
    update: ScanNotice | null | ((n: ScanNotice | null) => ScanNotice | null)
  ) => void;
  setEnhancedScansRemaining: (remaining: number | null) => void;
  showBanner: (config: BannerConfig) => void;
  bumpUsage: () => void;
  drainQueue: () => Promise<void>;
  imageInFlight: () => boolean;
  retryScan: (jobId: string) => void;
  queueForLater: (jobId: string) => void;
  requestEnhancedScan: (jobId: string) => void;
  restartScanSchedule: (jobId: string) => Promise<void>;
  /** Enhanced scans left as of the provider render that built these deps. */
  enhancedScansRemaining: number | null;
  t: TFn;
  /** Read at call time for the locale. */
  i18n: { language: string };
  jobsRef: MutableRefObject<ScanJob[]>;
  inFlightRef: MutableRefObject<Set<string>>;
  abortersRef: MutableRefObject<Set<AbortController>>;
  generationRef: MutableRefObject<number>;
  drainingRef: MutableRefObject<boolean>;
  drainAgainRef: MutableRefObject<boolean>;
  stopDrainRef: MutableRefObject<'none' | 'enhanced' | 'images'>;
  getTurnstileTokenRef: MutableRefObject<(signal?: AbortSignal) => Promise<string | null>>;
  turnstileConfiguredRef: MutableRefObject<boolean>;
  /** AI consent (#1023): no photo is uploaded until it stands. */
  aiConsentRef: MutableRefObject<ScanAiConsentGate>;
}
