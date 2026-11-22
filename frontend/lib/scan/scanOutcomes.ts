import { openScanQueue } from '../scanQueueNav';
import { Sentry } from '../sentry';
import {
  enhancedRefusalMessage,
  isTurnstileCode,
  MAX_SCAN_ATTEMPTS,
  SCAN_RETRY_DELAYS_MS,
  toScanNotice,
  type ScanUiState,
} from '../scanErrors';
import { isAwaitingRetry, isPendingUpload, type ScanJob } from '../scanJob';
import type { ScanExecDeps } from './types';

/**
 * The review sheet closes when an enhanced re-scan starts, and no screen
 * lists jobs, so reopen it on the earlier results or they'd be unreachable.
 */
export function restorePriorResults(
  deps: ScanExecDeps,
  job: ScanJob,
  updates: Partial<ScanJob>,
  message: string | null,
  type: 'info' | 'error'
) {
  deps.updateJob(job.id, { status: 'complete', error: undefined, ...updates });
  deps.setReviewingJobId(job.id);
  if (message) deps.showBanner({ message, type, duration: 6000 });
}

/**
 * A refusal from POST /scan (limit, busy, paused). Not a failure worth a
 * Sentry event or a retry prompt: the screen shows a notice and the job
 * stops (the drain halts at the first refusal, so it can't spam the API).
 */
export function handleScanRefusal(deps: ScanExecDeps, job: ScanJob, state: ScanUiState) {
  const notice = toScanNotice(state, Date.now(), true);
  if (!notice) return;
  // A refusal applies to every queued photo (and a busy lease means the
  // next one would be refused too), so stop sending photos; an
  // enhanced-only switch-off stops enhanced photos only.
  const scope = state.kind === 'paused' && state.enhancedOnly ? 'enhanced' : 'images';
  if (deps.stopDrainRef.current !== 'images') deps.stopDrainRef.current = scope;
  Sentry.addBreadcrumb({
    category: 'scan',
    message: `Scan refused: ${state.kind}`,
    level: 'info',
    data: { jobId: job.id },
  });
  // A refused enhanced re-scan must not strand the standard scan's books.
  // The notice card speaks, so no banner with the same text.
  if (job.enhanced && job.results?.length) {
    deps.setScanNotice(notice);
    restorePriorResults(deps, job, { enhanced: false }, null, 'info');
    return;
  }
  deps.setScanNotice(notice);
  // A refusal isn't a failure: the photo goes back in the queue (so it is
  // counted by the pending-uploads badge, survives a restart and is sent
  // by the next drain or by "Try again"). It is not retried within this
  // drain, which stops here, so a refusal costs at most one request.
  // A refusal ends any retry schedule: the photo starts again at attempt 1.
  deps.updateJob(job.id, {
    status: 'queued',
    error: state.kind,
    retryCount: 0,
    attempts: undefined,
    nextAttemptAt: undefined,
  });
}

/**
 * A retryable outcome (#604: 503 scan_unavailable, the provider was out
 * or sent a malformed reply; refunded server-side). The photo waits for
 * its next attempt on SCAN_RETRY_DELAYS_MS, and the drain sends no other
 * photo until then. After MAX_SCAN_ATTEMPTS it is failed.
 */
export function handleUnavailable(
  deps: ScanExecDeps,
  job: ScanJob,
  attempt: number,
  state: Extract<ScanUiState, { kind: 'unavailable' }>
) {
  Sentry.addBreadcrumb({
    category: 'scan',
    message: `Scan unavailable: attempt ${attempt}`,
    level: 'info',
    data: { jobId: job.id, attempt },
  });
  // The short notice is only for the first retry; no message per later retry.
  // It stays while another photo's first retry is pending (a capture sent
  // during a wait that got 503 too starts its own schedule).
  const clearRetryNotice = () => {
    const otherFirstRetryPending = deps.jobsRef.current.some(
      (j) => j.id !== job.id && isAwaitingRetry(j) && j.attempts === 1
    );
    if (!otherFirstRetryPending) deps.setScanNotice((n) => (n?.kind === 'paused' ? null : n));
  };
  if (attempt >= MAX_SCAN_ATTEMPTS) {
    deps.updateJob(job.id, {
      status: 'failed',
      error: 'unavailable',
      attempts: attempt,
      nextAttemptAt: undefined,
    });
    clearRetryNotice();
    // The photo no longer holds the queue: the drain sending it (which
    // never stopped, see below) goes on with the rest, and if none is
    // running one starts.
    const othersWaiting = deps.jobsRef.current.some(
      (j) => j.id !== job.id && isPendingUpload(j) && !deps.inFlightRef.current.has(j.id)
    );
    if (othersWaiting && !deps.drainingRef.current) void deps.drainQueue();
    // No image, URI or user content: just the counts and the code.
    Sentry.captureMessage('Scan failed after all retries', {
      level: 'warning',
      fingerprint: ['scan-final-failure'],
      tags: { feature: 'scan', attempts: String(attempt), last_error: state.code },
    });
    deps.showBanner({
      message: deps.t('scanFailedFinal'),
      type: 'error',
      duration: 8000,
      actions: [
        { label: deps.t('scanFailedFinalRetry'), onPress: () => deps.restartScanSchedule(job.id) },
        { label: deps.t('scanFailedFinalViewQueue'), onPress: openScanQueue },
      ],
    });
    return;
  }
  // The photo holds the queue until its next attempt: this drain sends no
  // more photos (text searches still go).
  deps.stopDrainRef.current = 'images';
  deps.updateJob(job.id, {
    status: 'queued',
    error: 'unavailable',
    retryCount: 0,
    attempts: attempt,
    nextAttemptAt: Date.now() + SCAN_RETRY_DELAYS_MS[attempt - 1],
  });
  if (attempt === 1) {
    const notice = toScanNotice(state);
    if (notice) deps.setScanNotice(notice);
  } else {
    clearRetryNotice();
  }
}

/**
 * An enhanced scan the server refused (#533: not Premium, or throttled
 * after repeated empty enhanced scans). Only enhanced photos are affected:
 * the drain stops sending enhanced photos (standard ones still go), the offer
 * is hidden until the server reports enhanced scans left again, and the
 * standard scan's books come back.
 */
export function handleEnhancedRefusal(
  deps: ScanExecDeps,
  job: ScanJob,
  state: Extract<ScanUiState, { kind: 'enhanced_refused' }>
) {
  if (deps.stopDrainRef.current === 'none') deps.stopDrainRef.current = 'enhanced';
  deps.setEnhancedScansRemaining(0);
  Sentry.addBreadcrumb({
    category: 'scan',
    message: `Enhanced scan refused: ${state.reason}`,
    level: 'info',
    data: { jobId: job.id },
  });
  const message = enhancedRefusalMessage(state, deps.t, deps.i18n.language);
  // Fall back to whatever the standard scan found, if anything.
  if (job.results?.length) {
    restorePriorResults(deps, job, { enhanced: false }, message, 'info');
  } else {
    deps.updateJob(job.id, { status: 'failed', enhanced: false, error: 'no_results' });
    deps.showBanner({ message, type: 'info', duration: 6000 });
  }
}

/** True when the failure has no response at all: the OS most likely suspended the upload (#213). */
export function isNetworkSuspend(err: unknown): boolean {
  return (
    err != null &&
    typeof err === 'object' &&
    'response' in err &&
    (err as { response: unknown }).response == null
  );
}

/**
 * The UI state for a failed send. #740: the server wants a token this build
 * can never send (no site key or base URL), so retrying cannot help: a final
 * failure now, not the retry schedule. A rejected token (403) stays retryable.
 */
export function classifyJobFailure(
  classified: ScanUiState,
  turnstileConfigured: boolean
): ScanUiState {
  return classified.kind === 'unavailable' &&
    classified.code === 'turnstile_missing' &&
    !turnstileConfigured
    ? { kind: 'generic' }
    : classified;
}

/**
 * Whether a failed send may have moved the usage counters. The server
 * answered, so a scan may have been counted (or refunded). Not for
 * paused/in_progress (nothing reserved), and not for network errors.
 */
export function shouldBumpUsageOnError(job: ScanJob, err: unknown, uiState: ScanUiState): boolean {
  return (
    job.type === 'image' &&
    (err as { response?: unknown } | null)?.response != null &&
    uiState.kind !== 'paused' &&
    // #604: refunded server-side. A Turnstile refusal (#740) is not, but
    // it was counted as a /scan request (#586), so it refreshes.
    (uiState.kind !== 'unavailable' || isTurnstileCode(uiState.code)) &&
    uiState.kind !== 'in_progress' &&
    uiState.kind !== 'enhanced_refused' &&
    // #586: refused before anything was taken; the counters are unchanged.
    uiState.kind !== 'requests_daily_limit'
  );
}
