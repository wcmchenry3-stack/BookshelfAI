import type { AxiosRequestHeaders } from 'axios';
import { Platform } from 'react-native';

import type { EnrichedBook } from '../../components/BookCandidatePicker';
import { api } from '../api';
import { SCAN_UPLOAD_TIMEOUT_MS, SEARCH_TIMEOUT_MS } from '../apiTimeouts';
import {
  apiErrorTags,
  isReportedCentrally,
  isServiceUnavailable,
  sanitizeApiError,
} from '../apiErrorReporting';
import {
  classifyScanError,
  MAX_IN_PROGRESS_RETRIES,
  MAX_SCAN_ATTEMPTS,
  SCAN_RETRY_DELAYS_MS,
  type ScanUiState,
} from '../scanErrors';
import { deleteScanImage } from '../scanJobStorage';
import {
  isAwaitingRetry,
  noMatchMessage,
  repeatOutcome,
  type RepeatOutcome,
  type ScanJob,
  type ScanResponse,
} from '../scanJob';
import { Sentry } from '../sentry';
import { TURNSTILE_FORM_FIELD } from '../turnstile/config';
import { withScanRecord } from '../unverified';
import {
  classifyJobFailure,
  handleEnhancedRefusal,
  handleScanRefusal,
  handleUnavailable,
  isNetworkSuspend,
  restorePriorResults,
  shouldBumpUsageOnError,
} from './scanOutcomes';
import type { ScanExecDeps } from './types';

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Send one scan job (a text search or a photo) and settle it. Plain async
 * function: ScanJobProvider rebuilds `deps` after the renders that change
 * its callbacks and passes it here, so nothing is frozen.
 */
export async function executeScan(deps: ScanExecDeps, job: ScanJob): Promise<void> {
  // Synchronous (before any await) so a drain can't start the same job.
  deps.inFlightRef.current.add(job.id);
  const controller = new AbortController();
  deps.abortersRef.current.add(controller);
  try {
    await runScan(deps, job, controller.signal);
  } finally {
    deps.abortersRef.current.delete(controller);
    deps.inFlightRef.current.delete(job.id);
    // A retry queued behind this send (outside a drain; a drain handles
    // it when it ends). Left for a photo still being sent, so a text
    // search finishing first doesn't send the retry alongside it.
    if (deps.drainAgainRef.current && !deps.drainingRef.current && !deps.imageInFlight()) {
      deps.drainAgainRef.current = false;
      void deps.drainQueue();
    }
  }
}

/** One POST /scan form body; each one carries its own Turnstile token (#740). */
function buildScanFormData(job: ScanJob, attempt: number, turnstileToken: string | null) {
  const formData = new FormData();
  if (job.enhanced) formData.append('enhanced', 'true');
  // The server accepts 1-6 (#603) and ignores it until that ships.
  formData.append('attempt', String(attempt));
  // Single-use, so never reused across posts. Missing (no site key,
  // widget timeout or error): sent without it, which a server with
  // TURNSTILE_REQUIRED=false accepts.
  if (turnstileToken) formData.append(TURNSTILE_FORM_FIELD, turnstileToken);
  if (Platform.OS === 'web') {
    formData.append('file', job.imageUri as unknown as Blob, 'scan.jpg');
  } else {
    formData.append('file', {
      uri: job.imageUri,
      name: 'scan.jpg',
      type: 'image/jpeg',
    } as unknown as Blob);
  }
  return formData;
}

async function runScan(deps: ScanExecDeps, job: ScanJob, signal: AbortSignal) {
  const { updateJob, showBanner, bumpUsage, setScanNotice, setReviewingJobId, t } = deps;
  const generation = deps.generationRef.current;
  const signedOutSince = () => deps.generationRef.current !== generation;
  updateJob(job.id, { status: 'searching' });
  // Enhanced scans left as reported by this response (the state update
  // lands next render).
  let reportedRemaining: number | null = deps.enhancedScansRemaining;
  // Whether a no-match used a scan, as the server reports it (#577).
  let noMatch: ScanResponse['no_match'] = null;
  // How the server says this photo relates to an earlier scan (#588).
  let repeat: RepeatOutcome = 'none';
  // This request's attempt on the retry schedule (#604), 1 for a first try.
  // The cap is a backstop only: no path sends a job whose attempts are
  // used up. After attempt MAX_SCAN_ATTEMPTS the job is `failed` with no
  // nextAttemptAt (the drain sends only queued/pending jobs), and the
  // banner's Retry (restartScanSchedule) clears `attempts` first.
  const attempt = Math.min((job.attempts ?? 0) + 1, MAX_SCAN_ATTEMPTS);

  try {
    let results: EnrichedBook[];

    if (job.type === 'text') {
      const response = await api.get<EnrichedBook[]>('/books/search', {
        params: { q: job.query },
        signal,
        timeout: SEARCH_TIMEOUT_MS,
      });
      if (signedOutSince()) return;
      results = response.data ?? [];
    } else {
      const postScan = async () => {
        const turnstileToken = await deps.getTurnstileTokenRef.current(signal);
        // Signed out while the token was being fetched: nothing is sent,
        // and the token is never used for the next account.
        if (signedOutSince() || signal.aborted) return null;
        return api.post<ScanResponse>('/scan', buildScanFormData(job, attempt, turnstileToken), {
          signal,
          timeout: SCAN_UPLOAD_TIMEOUT_MS,
          transformRequest: [
            (data: FormData, headers: AxiosRequestHeaders) => {
              if (Platform.OS === 'web') {
                headers.delete('Content-Type');
              } else {
                headers.set('Content-Type', 'multipart/form-data');
              }
              return data;
            },
          ],
        });
      };
      // 429 scan_in_progress means another scan holds the user's lease:
      // wait Retry-After and retry silently, at most MAX_IN_PROGRESS_RETRIES
      // times. If it persists the error propagates and the catch below
      // shows the neutral "Still processing" notice.
      let response!: NonNullable<Awaited<ReturnType<typeof postScan>>>;
      for (let attempt = 0, done = false; !done; attempt++) {
        // Checked before every post, retries included.
        if (signedOutSince()) return;
        try {
          const answered = await postScan();
          if (!answered) return;
          response = answered;
          done = true;
        } catch (postErr) {
          if (signedOutSince()) return;
          const state = classifyScanError(postErr);
          if (state.kind !== 'in_progress' || attempt >= MAX_IN_PROGRESS_RETRIES) {
            throw postErr;
          }
          await sleep(state.retryAfterSeconds * 1000);
        }
      }
      // Before any state update: the answer belongs to the old account.
      if (signedOutSince()) return;
      // The server answered: any retry schedule is over.
      if (job.attempts != null || job.nextAttemptAt != null) {
        updateJob(job.id, { attempts: undefined, nextAttemptAt: undefined });
      }
      repeat = repeatOutcome(response.data);
      // A scan reached the server, so the usage counters are stale. A
      // repeat is answered from the saved record and charges nothing, so
      // the counters have not moved (#588).
      if (repeat === 'none') bumpUsage();
      setScanNotice(null);
      // The service evidently accepts scans again: send any scans that were
      // refused earlier. Never re-entrant (drainingRef), and a drain sends
      // each job once and stops at the first refusal, so this can't loop.
      // A photo waiting on the retry schedule (#604) is made due now,
      // keeping its attempt count: the service is evidently back.
      const now = Date.now();
      const pullForward = (j: ScanJob) =>
        j.id !== job.id && isAwaitingRetry(j) && j.nextAttemptAt! > now;
      if (deps.jobsRef.current.some(pullForward)) {
        deps.setJobsAndRef((list) =>
          list.map((j) => (pullForward(j) ? { ...j, nextAttemptAt: now } : j))
        );
      }
      const waiting = deps.jobsRef.current.some(
        (j) =>
          j.id !== job.id &&
          (j.status === 'queued' || j.status === 'pending') &&
          !deps.inFlightRef.current.has(j.id)
      );
      if (waiting && !deps.drainingRef.current) void deps.drainQueue();
      // Each result keeps the scan record it came from (#631), so an
      // "Add it yourself" form pre-filled from it can name it, even
      // after the job was queued, retried or reopened.
      results = withScanRecord(response.data?.books ?? [], response.data?.scan_record_id);
      const reportedNoMatch = response.data?.no_match;
      if (reportedNoMatch === 'free' || reportedNoMatch === 'counted') {
        noMatch = reportedNoMatch;
      }
      // A number for Premium, null otherwise (enhanced is Premium-only).
      const remaining = response.data?.enhanced_scans_remaining;
      if (typeof remaining === 'number' || remaining === null) {
        reportedRemaining = remaining;
        deps.setEnhancedScansRemaining(remaining);
      }
    }

    if (results.length === 0) {
      // An enhanced scan that found nothing leaves the earlier results intact.
      if (job.enhanced && job.results?.length) {
        restorePriorResults(deps, job, { enhanced: true }, t('enhancedNoNewBooks'), 'info');
        return;
      }
      updateJob(job.id, { status: 'failed', error: 'no_results' });
      // Offer the stronger model once, unless this already was the enhanced
      // pass, and only when the server says the user has enhanced scans
      // left (never for Free users, who have none).
      const canEnhance =
        job.type === 'image' &&
        !job.enhanced &&
        reportedRemaining !== null &&
        reportedRemaining > 0;
      showBanner({
        message: job.enhanced
          ? t('enhancedNoBooksFound')
          : repeat === 'no_match'
            ? t('repeatNoMatch')
            : noMatchMessage(noMatch, t('noBooksFoundTitle'), t),
        type: 'info',
        duration: canEnhance ? 8000 : 4000,
        actions: canEnhance
          ? [{ label: t('tryEnhanced'), onPress: () => deps.requestEnhancedScan(job.id) }]
          : undefined,
      });
      return;
    }

    // A repeat whose books are all in the library: just say so (#588).
    // An enhanced re-scan can be a repeat too (a photo already enhanced).
    if (repeat === 'all_in_library') {
      // Discard it here with the job in hand: the jobs list this closure
      // or a ref can see may not hold it yet, which would keep the photo
      // on disk.
      deps.setJobsAndRef((prev) => prev.filter((j) => j.id !== job.id));
      if (job.imageUri) deleteScanImage(job.imageUri);
      showBanner({
        message: results.length === 1 ? t('repeatAlreadyHaveOne') : t('repeatAlreadyHaveMany'),
        type: 'info',
        duration: 5000,
      });
      return;
    }

    // Keep the image: the user may still ask for an enhanced re-scan from the
    // review sheet. It's deleted when the job is dismissed.
    updateJob(job.id, { status: 'complete', results, error: undefined });
    // A repeat with books missing from the library: offer to re-add from
    // the saved result (the review sheet, then POST /wishlist only) (#588).
    if (repeat === 'not_in_library') {
      showBanner({
        message: t('repeatNotInLibrary'),
        type: 'info',
        duration: 8000,
        actions: [{ label: t('repeatReAdd'), onPress: () => setReviewingJobId(job.id) }],
      });
      return;
    }
    showBanner({
      message:
        job.type === 'image'
          ? t('booksFound', { count: results.length })
          : t('bookFound', { title: results[0].title }),
      type: 'success',
      actions: [
        {
          label: t('viewResults'),
          onPress: () => setReviewingJobId(job.id),
        },
      ],
    });
  } catch (err) {
    if (signedOutSince()) return;
    // A status-0 / no-response error almost always means the OS suspended
    // the upload mid-flight (app backgrounded). Auto-queue so it drains
    // on return instead of asking the user to manually retry. (#213)
    if (isNetworkSuspend(err)) {
      Sentry.addBreadcrumb({
        category: 'scan',
        message: 'Scan upload interrupted by backgrounding — auto-queued',
        level: 'warning',
        data: { jobId: job.id },
      });
      // No response is not a failed attempt (#604): the attempt count
      // stays. A photo on the retry schedule waits at least the first
      // step again, so a device that still reports "connected" can't
      // resend it in a tight loop.
      updateJob(
        job.id,
        job.attempts != null
          ? { status: 'queued', nextAttemptAt: Date.now() + SCAN_RETRY_DELAYS_MS[0] }
          : { status: 'queued' }
      );
      showBanner({
        message: t('savedOffline'),
        type: 'info',
        duration: 4000,
      });
      return;
    }

    const classified: ScanUiState =
      job.type === 'image' ? classifyScanError(err) : { kind: 'generic' };
    const uiState = classifyJobFailure(classified, deps.turnstileConfiguredRef.current);

    if (shouldBumpUsageOnError(job, err, uiState)) bumpUsage();

    if (
      uiState.kind === 'daily_limit' ||
      uiState.kind === 'requests_daily_limit' ||
      uiState.kind === 'in_progress' ||
      uiState.kind === 'paused'
    ) {
      handleScanRefusal(deps, job, uiState);
      return;
    }

    if (uiState.kind === 'unavailable') {
      // An enhanced re-scan is never retried: like before #604 it takes
      // the paused path (its earlier books back if it has any, otherwise
      // queued with the paused notice). No schedule, banner or Sentry event.
      if (job.enhanced) {
        handleScanRefusal(deps, job, { kind: 'paused' });
      } else {
        handleUnavailable(deps, job, attempt, uiState);
      }
      return;
    }

    if (job.enhanced && uiState.kind === 'enhanced_refused') {
      handleEnhancedRefusal(deps, job, uiState);
      return;
    }

    // #724: the central API reporter (lib/api.ts) owns 403/5xx responses;
    // capture only what it does not (no response, other statuses).
    if (!isReportedCentrally(err)) {
      Sentry.captureException(sanitizeApiError(err), {
        tags: {
          feature: 'scan',
          action: 'execute_scan',
          jobType: job.type,
          ...apiErrorTags(err),
        },
      });
    }

    // A failed enhanced re-scan must not strand the books the standard scan
    // already found — a failed job has no review sheet.
    if (job.enhanced && job.results?.length) {
      restorePriorResults(deps, job, { enhanced: false }, t('enhancedFailed'), 'error');
      return;
    }
    updateJob(job.id, {
      status: 'failed',
      error: 'network_or_server',
      attempts: undefined,
      nextAttemptAt: undefined,
    });
    showBanner({
      message: isServiceUnavailable(err)
        ? t('serviceUnavailable', { ns: 'common' })
        : t('scanFailedTitle'),
      type: 'error',
      actions: [
        { label: t('retryNow'), onPress: () => deps.retryScan(job.id) },
        { label: t('saveForLater'), onPress: () => deps.queueForLater(job.id) },
      ],
      duration: 8000,
    });
  }
}
