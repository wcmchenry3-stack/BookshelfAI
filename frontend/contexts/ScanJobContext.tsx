import React, { createContext, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Platform } from 'react-native';
import NetInfo from '@react-native-community/netinfo';
import * as Crypto from 'expo-crypto';
import { useTranslation } from 'react-i18next';

import type { EnrichedBook } from '../components/BookCandidatePicker';
import { useBanner } from '../hooks/useBanner';
import { api } from '../lib/api';
import { catalogRefusal, isAlreadyOwnedError } from '../lib/ownership';
import { openScanQueue } from '../lib/scanQueueNav';
import {
  apiErrorTags,
  isReportedCentrally,
  isServiceUnavailable,
  sanitizeApiError,
} from '../lib/apiErrorReporting';
import { Sentry } from '../lib/sentry';
import {
  classifyScanError,
  enhancedRefusalMessage,
  hasRefusedJobs,
  MAX_IN_PROGRESS_RETRIES,
  MAX_SCAN_ATTEMPTS,
  MAX_SCAN_RETRY_DELAY_MS,
  SCAN_RETRY_DELAYS_MS,
  toScanNotice,
  type ScanNotice,
  type ScanUiState,
} from '../lib/scanErrors';
import {
  isAwaitingRetry,
  isFailedScanExpired,
  isPendingUpload,
  MAX_QUEUE_SIZE,
  noMatchMessage,
  repeatOutcome,
  type RepeatOutcome,
  type ScanJob,
  type ScanJobType,
  type ScanResponse,
} from '../lib/scanJob';
import { deleteScanImage, loadJobs, saveJobs, sweepOrphanedScanFiles } from '../lib/scanJobStorage';
import { draftFromBook, withScanRecord, type UnverifiedDraft } from '../lib/unverified';

/** An "Add it yourself" form waiting to be shown, with its own id (#616). */
export type PendingUnverifiedDraft = UnverifiedDraft & { id: string };

const MAX_RETRIES = 3;
const QUEUE_DRAIN_DELAY = 2000;
// Extra drain passes when a retry wait ends mid-drain (#604).
const MAX_DRAIN_PASSES = 3;
// When the retry timer fires during a drain, check again after this long.
const RETRY_TIMER_BUSY_DELAY = 1000;
// When the retry timer fires while offline, check again after this long (#604).
const RETRY_TIMER_OFFLINE_DELAY = 60 * 1000;

export interface ScanJobContextValue {
  jobs: ScanJob[];
  /** Scans waiting to upload (queued offline or pending). */
  pendingCount: number;
  /** True when the queue is at MAX_QUEUE_SIZE and no more captures can be accepted. */
  isQueueFull: boolean;
  reviewingJob: ScanJob | null;
  /**
   * Premium enhanced scans left today (the shared daily allowance left, #582),
   * as last reported by a scan response; null when unknown or the user is not
   * Premium. The enhanced re-scan is offered only when this is > 0.
   */
  enhancedScansRemaining: number | null;
  /** Why the last scan was refused (limit reached, paused, ...); null when there is nothing to show. */
  scanNotice: ScanNotice | null;
  dismissScanNotice: () => void;
  /** "Try again": clear the notice and send the queued scans in order (stops at the next refusal). */
  resumeQueue: () => void;
  /** Changes whenever a scan reached the server; usage counters refetch on change. */
  usageRefreshKey: number;
  startScan: (type: ScanJobType, imageUri?: string, query?: string) => void;
  retryScan: (jobId: string) => void;
  queueForLater: (jobId: string) => void;
  reviewJob: (jobId: string) => void;
  dismissReview: () => void;
  dismissJob: (jobId: string) => void;
  /** Send a failed photo again now, with the retry schedule restarted at attempt 1 (#605). */
  restartScanSchedule: (jobId: string) => Promise<void>;
  /** Remove failed scans older than 7 days, with their images (#605). */
  purgeExpiredFailedJobs: () => void;
  /** Re-run an image scan with the stronger model (uses an enhanced scan if it finds books). */
  requestEnhancedScan: (jobId: string) => void;
  /** Add the books ticked in the review checklist to the wishlist. */
  handleAddBooks: (books: EnrichedBook[]) => Promise<void>;
  /**
   * "Add it yourself" forms still to show (#616), one per book the catalog
   * could not find, pre-filled from the scan; the first is shown.
   */
  unverifiedDrafts: PendingUnverifiedDraft[];
  /** Open the "Add it yourself" form for these books (no catalog match). */
  addYourself: (books: EnrichedBook[]) => void;
  /**
   * A form was saved: take its book out of the scan it came from, show the
   * next form, if any, and confirm. `draftId` is the draft the save started
   * from (default: the one shown), so a save that completes after its form
   * was closed never touches another draft.
   */
  unverifiedSaved: (title: string, draftId?: string) => void;
  /** Close the "Add it yourself" forms. */
  dismissUnverified: () => void;
}

export const ScanJobContext = createContext<ScanJobContextValue>({
  jobs: [],
  pendingCount: 0,
  isQueueFull: false,
  reviewingJob: null,
  enhancedScansRemaining: null,
  scanNotice: null,
  dismissScanNotice: () => {},
  resumeQueue: () => {},
  usageRefreshKey: 0,
  startScan: () => {},
  retryScan: () => {},
  queueForLater: () => {},
  reviewJob: () => {},
  dismissReview: () => {},
  dismissJob: () => {},
  restartScanSchedule: async () => {},
  purgeExpiredFailedJobs: () => {},
  requestEnhancedScan: () => {},
  handleAddBooks: async () => {},
  unverifiedDrafts: [],
  addYourself: () => {},
  unverifiedSaved: () => {},
  dismissUnverified: () => {},
});

/** Identifies a scan result within its job (title and author, as read). */
function candidateKey(book: EnrichedBook): string {
  return `${book.title}\u0000${book.author}`;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function ScanJobProvider({ children }: { children: React.ReactNode }) {
  const [jobs, setJobs] = useState<ScanJob[]>([]);
  const [reviewingJobId, setReviewingJobId] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [enhancedScansRemaining, setEnhancedScansRemaining] = useState<number | null>(null);
  const [scanNotice, setScanNotice] = useState<ScanNotice | null>(null);
  const [usageRefreshKey, setUsageRefreshKey] = useState(0);
  const [unverifiedDrafts, setUnverifiedDrafts] = useState<PendingUnverifiedDraft[]>([]);
  const reviewingJobIdRef = useRef<string | null>(null);
  // The job whose review to reopen after the "Add it yourself" forms close.
  const reopenReviewJobIdRef = useRef<string | null>(null);
  // Where each draft's book came from, by draft id. Kept after the forms
  // close, so a save still in flight can take its own book out of its scan.
  const draftSourcesRef = useRef(new Map<string, { jobId: string | null; key: string }>());
  const nextDraftIdRef = useRef(0);
  // Keep jobs in a ref so callbacks can read the latest state synchronously.
  // The ref is the source of truth: every change goes through setJobsAndRef,
  // which updates the ref first and then hands React the same list. There is
  // no effect copying `jobs` back into the ref, because a render with an older
  // `jobs` could overwrite a newer ref (a drain started by the retry timer,
  // #604, would then resend a job that just settled).
  const jobsRef = useRef(jobs);
  const setJobsAndRef = useCallback((updater: (prev: ScanJob[]) => ScanJob[]) => {
    const next = updater(jobsRef.current);
    jobsRef.current = next;
    setJobs(next);
  }, []);

  const drainingRef = useRef(false);
  // A retried photo was queued while another photo was being sent or a drain
  // was running: run another drain as soon as that finishes (#605).
  const drainAgainRef = useRef(false);
  // Which photos the current drain stops sending after a refusal: all of
  // them (limit, paused, busy) or only enhanced ones (enhanced switched off).
  const stopDrainRef = useRef<'none' | 'enhanced' | 'images'>('none');
  // Ids of jobs being sent right now, so a drain can't send the same job twice.
  const inFlightRef = useRef<Set<string>>(new Set());
  // Whether a photo is being sent right now.
  const imageInFlight = useCallback(
    () =>
      [...inFlightRef.current].some(
        (id) => jobsRef.current.find((j) => j.id === id)?.type === 'image'
      ),
    []
  );
  // Whether a photo is being uploaded right now (past its connectivity
  // check), which a drain must not send another photo alongside.
  const imageUploading = useCallback(
    () =>
      [...inFlightRef.current].some((id) => {
        const j = jobsRef.current.find((x) => x.id === id);
        return j?.type === 'image' && j.status === 'searching';
      }),
    []
  );
  const executeScanRef = useRef<(job: ScanJob) => Promise<void>>(async () => {});
  // Set when the initial load fails, so the persist effect below doesn't
  // immediately overwrite storage with the empty `jobs` state it starts
  // with — we don't know what was actually stored. Consumed on first fire.
  const skipNextPersistRef = useRef(false);
  const { showBanner } = useBanner();
  const { t, i18n } = useTranslation('scan');

  const dismissScanNotice = useCallback(() => setScanNotice(null), []);
  const bumpUsage = useCallback(() => setUsageRefreshKey((k) => k + 1), []);

  // Persist jobs whenever they change (after initial load).
  useEffect(() => {
    if (!loaded) return;
    if (skipNextPersistRef.current) {
      skipNextPersistRef.current = false;
      return;
    }
    saveJobs(jobs);
  }, [jobs, loaded]);

  // Load persisted jobs on mount. Reset interrupted searches to pending, then
  // remove scan-queue files that no persisted job refers to (crashed captures).
  useEffect(() => {
    (async () => {
      const launchedAt = Date.now();
      const persisted = await loadJobs();
      if (persisted === null) {
        // Storage read/parse failed — we can't tell which images in
        // scan-queue/ are still owned by a job, so don't guess: skip the
        // sweep entirely rather than risk deleting un-uploaded photos.
        Sentry.addBreadcrumb({
          category: 'scan',
          message: 'Failed to load persisted scan jobs — skipping orphan sweep',
          level: 'warning',
        });
        skipNextPersistRef.current = true;
        setLoaded(true);
        return;
      }

      // Completed jobs are persisted without their results (see saveJobs), so
      // there is nothing left to review — drop them. The sweep below then
      // removes their images, since no remaining job refers to them.
      // A next try time beyond the longest wait (#604: the clock moved back,
      // or corrupt data) is brought forward to it.
      const latestRetryAt = launchedAt + MAX_SCAN_RETRY_DELAY_MS;
      const restored = persisted
        .filter((j) => j.status !== 'complete')
        .map((j) => (j.status === 'searching' ? { ...j, status: 'pending' as const } : j))
        .map((j) =>
          j.nextAttemptAt != null && j.nextAttemptAt > latestRetryAt
            ? { ...j, nextAttemptAt: latestRetryAt }
            : j
        );
      // Failed scans older than 7 days are dropped (#605). Their images are
      // not deleted here: the sweep below removes files no kept job refers to
      // (they are older than `launchedAt`, so the sweep's age guard allows it),
      // so a job is never saved with its image already gone.
      const kept = restored.filter((j) => !isFailedScanExpired(j, launchedAt));
      setJobsAndRef(() => kept);
      setLoaded(true);
      await sweepOrphanedScanFiles(kept, launchedAt);
    })();
  }, [setJobsAndRef]);

  const updateJob = useCallback(
    (jobId: string, updates: Partial<ScanJob>) =>
      setJobsAndRef((list) =>
        list.map((j) => {
          if (j.id !== jobId) return j;
          // Record when a job became failed (#605), only on the change to
          // failed; a job that leaves `failed` loses it. A job saved before
          // `failedAt` existed has none and counts from `createdAt`.
          const failedAt =
            updates.status === 'failed'
              ? j.status === 'failed'
                ? (updates.failedAt ?? j.failedAt)
                : (updates.failedAt ?? Date.now())
              : updates.status
                ? undefined
                : j.failedAt;
          return { ...j, ...updates, failedAt };
        })
      ),
    [setJobsAndRef]
  );

  // Drain queued/pending jobs when connectivity is restored. Declared before
  // the connectivity effect to satisfy no-use-before-define. Only uses stable
  // refs and setJobs so deps are empty.
  const drainQueue = useCallback(async () => {
    // Runs again when a retry was queued while it was running (#605).
    do {
      drainAgainRef.current = false;
      drainingRef.current = true;
      try {
        // A photo skipped only because another photo was waiting for its
        // retry is revisited once that wait ends during the pass (the waiting
        // photo failed for good or was sent and succeeded), since nothing
        // else would start another drain then (#604). Bounded: each extra
        // pass needs a photo to have stopped waiting during the previous one.
        for (let pass = 0; pass < MAX_DRAIN_PASSES; pass++) {
          if (!(await drainPass())) break;
        }
      } finally {
        drainingRef.current = false;
      }
      // Left for the photo being sent, whose completion starts the next drain.
    } while (drainAgainRef.current && !imageUploading());

    // One pass over the queue; true when a photo was skipped for another
    // photo's retry wait and no photo is waiting any more.
    async function drainPass(): Promise<boolean> {
      // Read the latest jobs from the ref: a setJobs updater is run lazily by
      // React, so it can't be used to read state synchronously.
      const pending = jobsRef.current.filter(
        (j) => j.status === 'queued' || j.status === 'pending'
      );
      // Retry schedule (#604): photos whose next attempt is due go first,
      // earliest first. While any photo is still waiting for its next
      // attempt, no other photo is sent (one at a time).
      const startedAt = Date.now();
      const due = pending
        .filter((j) => isAwaitingRetry(j) && j.nextAttemptAt! <= startedAt)
        .sort((a, b) => a.nextAttemptAt! - b.nextAttemptAt!);
      const queuedJobs = [...due, ...pending.filter((j) => !due.includes(j))];
      // Re-read on every step, not once: a photo can stop waiting during the
      // drain (failed after its last attempt, or made due by a success).
      const waitingForRetry = () => {
        const now = Date.now();
        return jobsRef.current.some(
          (j) =>
            j.type === 'image' &&
            isAwaitingRetry(j) &&
            j.nextAttemptAt! > now &&
            !inFlightRef.current.has(j.id)
        );
      };

      stopDrainRef.current = 'none';
      let skippedForWait = false;
      for (const job of queuedJobs) {
        const netState = await NetInfo.fetch();
        // Only a known-offline state stops the drain; null ("unknown") counts
        // as connected, as in handleAddBooks (#604).
        if (netState.isConnected === false) break;
        // The list above is a snapshot: skip jobs dismissed, sent or being
        // sent (startScan/retryScan) since.
        const current = jobsRef.current.find((j) => j.id === job.id);
        // Re-read: executeScan (via a refusal) sets it during the loop.
        const stop = stopDrainRef.current as 'none' | 'enhanced' | 'images';
        // Held back only because another photo is waiting for its retry.
        const heldByWait =
          !!current &&
          current.type === 'image' &&
          stop === 'none' &&
          !isAwaitingRetry(current) &&
          waitingForRetry();
        if (heldByWait) skippedForWait = true;
        // One photo at a time: a photo uploading outside this drain
        // (startScan, retryScan) holds back the drain's photos until it
        // finishes, which then drains again (#605).
        if (current?.type === 'image' && !inFlightRef.current.has(current.id) && imageUploading()) {
          drainAgainRef.current = true;
          continue;
        }
        if (
          !current ||
          (current.status !== 'queued' && current.status !== 'pending') ||
          inFlightRef.current.has(job.id) ||
          // A refusal (limit, paused) applies to photo uploads only (POST
          // /scan); queued text searches still go through.
          (current.type === 'image' &&
            (stop === 'images' ||
              (stop === 'enhanced' && !!current.enhanced) ||
              // Not due yet, or another photo is waiting for its retry.
              (isAwaitingRetry(current) ? current.nextAttemptAt! > Date.now() : heldByWait)))
        ) {
          continue;
        }
        await executeScanRef.current(current);
        // Delay between jobs to avoid API burst.
        if (queuedJobs.indexOf(job) < queuedJobs.length - 1) {
          await new Promise((r) => setTimeout(r, QUEUE_DRAIN_DELAY));
        }
      }
      const stopped = stopDrainRef.current as 'none' | 'enhanced' | 'images';
      return skippedForWait && stopped === 'none' && !waitingForRetry();
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const resumeQueue = useCallback(() => {
    setScanNotice(null);
    if (!drainingRef.current) void drainQueue();
  }, [drainQueue]);

  // Listen for connectivity changes and drain queued jobs.
  useEffect(() => {
    const unsubscribe = NetInfo.addEventListener((state) => {
      if (state.isConnected && !drainingRef.current) {
        drainQueue();
      }
    });
    return unsubscribe;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded]);

  // Retry schedule timer (#604): while the app is open, drain when the
  // earliest scheduled attempt is due. Re-armed whenever jobs change (a new
  // schedule, a sent or dismissed photo) and cleared on unmount. On launch
  // the connectivity listener's first event drains whatever is already due.
  // A wait is never armed for longer than the longest step of the schedule.
  // Known offline (isConnected === false), nothing is sent and no attempt is
  // used: it checks again after RETRY_TIMER_OFFLINE_DELAY (or on reconnect).
  // The drain never sends a job that is in flight (inFlightRef), so the timer
  // can't double-send.
  const nextRetryAt = useMemo(() => {
    let next: number | null = null;
    for (const j of jobs) {
      if (j.type === 'image' && isAwaitingRetry(j) && (next === null || j.nextAttemptAt! < next)) {
        next = j.nextAttemptAt!;
      }
    }
    return next;
  }, [jobs]);
  useEffect(() => {
    if (!loaded || nextRetryAt === null) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const arm = (delay: number) => {
      timer = setTimeout(() => void fire(), Math.min(Math.max(0, delay), MAX_SCAN_RETRY_DELAY_MS));
    };
    async function fire() {
      if (cancelled) return;
      // A timer can fire a little early; wait out the rest.
      const remaining = nextRetryAt! - Date.now();
      if (remaining > 0) return arm(remaining);
      if (drainingRef.current) return arm(RETRY_TIMER_BUSY_DELAY);
      const netState = await NetInfo.fetch();
      if (cancelled) return;
      if (netState.isConnected === false) return arm(RETRY_TIMER_OFFLINE_DELAY);
      if (drainingRef.current) return arm(RETRY_TIMER_BUSY_DELAY);
      await drainQueue();
      // A drain that sent the due photo changed the jobs, which re-ran this
      // effect (cancelled). If it could not (it went offline mid-drain), look
      // again later rather than wait for an event that may not come.
      if (!cancelled) arm(RETRY_TIMER_OFFLINE_DELAY);
    }
    arm(nextRetryAt - Date.now());
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [loaded, nextRetryAt, drainQueue]);

  const pendingCount = useMemo(() => jobs.filter(isPendingUpload).length, [jobs]);
  const isQueueFull = pendingCount >= MAX_QUEUE_SIZE;

  const startScan = useCallback(
    async (type: ScanJobType, imageUri?: string, query?: string) => {
      let startedId: string | null = null;
      try {
        // Backstop for the cap — the scan screen checks isQueueFull before
        // persisting a capture, but a stale render could still get here.
        if (jobsRef.current.filter(isPendingUpload).length >= MAX_QUEUE_SIZE) {
          if (imageUri) deleteScanImage(imageUri);
          showBanner({
            message: t(hasRefusedJobs(jobsRef.current) ? 'queueFullLimited' : 'queueFull'),
            type: 'error',
            duration: 4000,
          });
          return;
        }

        const job: ScanJob = {
          id: Crypto.randomUUID(),
          type,
          status: 'pending',
          // eslint-disable-next-line react-hooks/purity
          createdAt: Date.now(),
          query,
          imageUri,
          retryCount: 0,
        };

        Sentry.addBreadcrumb({
          category: 'scan',
          message: `Scan started: ${type}`,
          level: 'info',
          data: { jobId: job.id, type: job.type },
        });

        setJobsAndRef((prev) => [job, ...prev]);
        // Held across the connectivity check below, where the job is already
        // 'pending' but not yet sent: a reconnect drain must skip it.
        startedId = job.id;
        inFlightRef.current.add(job.id);

        const netState = await NetInfo.fetch();
        if (!netState.isConnected) {
          updateJob(job.id, { status: 'queued' });
          showBanner({
            message: t('savedOffline'),
            type: 'info',
            duration: 4000,
          });
          return;
        }

        await executeScanRef.current(job);
      } catch (err) {
        if (!isReportedCentrally(err)) {
          Sentry.captureException(sanitizeApiError(err));
        }
        showBanner({
          message: t('scanFailedMessage'),
          type: 'error',
          duration: 4000,
        });
      } finally {
        if (startedId) inFlightRef.current.delete(startedId);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [showBanner, t]
  );

  const retryScan = useCallback(
    async (jobId: string) => {
      const job = jobsRef.current.find((j) => j.id === jobId);
      if (!job) return;

      Sentry.addBreadcrumb({
        category: 'scan',
        message: `Scan retry: attempt ${job.retryCount + 1}`,
        level: 'info',
        data: { jobId, retryCount: job.retryCount + 1 },
      });

      if (job.retryCount >= MAX_RETRIES) {
        showBanner({
          message: t('maxRetries'),
          type: 'info',
          duration: 4000,
        });
        updateJob(jobId, { status: 'queued' });
        return;
      }

      const updated: ScanJob = {
        ...job,
        retryCount: job.retryCount + 1,
        status: 'pending' as const,
        error: undefined,
        failedAt: undefined,
      };
      setJobsAndRef((prev) => prev.map((j) => (j.id === jobId ? updated : j)));
      await executeScanRef.current(updated);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [showBanner, t]
  );

  // "Retry" on the final-failure banner (#604): send the photo again now,
  // with the schedule restarted at attempt 1.
  const restartScanSchedule = useCallback(
    async (jobId: string) => {
      const job = jobsRef.current.find((j) => j.id === jobId);
      if (!job || job.status !== 'failed' || inFlightRef.current.has(jobId)) return;
      // One photo at a time (#604): if another photo is being sent, a drain is
      // running, or a photo waits on its retry schedule, hand this one to the
      // drain instead of sending it now.
      const now = Date.now();
      const imageSending = imageInFlight();
      const photoWaiting = jobsRef.current.some(
        (j) => j.id !== jobId && j.type === 'image' && isAwaitingRetry(j) && j.nextAttemptAt! > now
      );
      if (job.type === 'image' && (imageSending || drainingRef.current || photoWaiting)) {
        setJobsAndRef((prev) =>
          prev.map((j) =>
            j.id === jobId
              ? {
                  ...j,
                  status: 'queued' as const,
                  error: undefined,
                  failedAt: undefined,
                  retryCount: 0,
                  attempts: undefined,
                  nextAttemptAt: undefined,
                }
              : j
          )
        );
        // Starting a drain now would send this photo alongside the one being
        // sent, so ask for one when that send or the running drain finishes.
        if (!imageSending && !drainingRef.current) void drainQueue();
        else drainAgainRef.current = true;
        return;
      }
      const updated: ScanJob = {
        ...job,
        status: 'pending',
        error: undefined,
        failedAt: undefined,
        retryCount: 0,
        attempts: undefined,
        nextAttemptAt: undefined,
      };
      setJobsAndRef((prev) => prev.map((j) => (j.id === jobId ? updated : j)));
      await executeScanRef.current(updated);
    },
    [setJobsAndRef, drainQueue, imageInFlight]
  );

  // Failed scans are kept 7 days (#605): on launch (the load above) and when
  // the queue screen opens, older ones are removed with their images.
  const purgeExpiredFailedJobs = useCallback(() => {
    const now = Date.now();
    const expired = jobsRef.current.filter((j) => isFailedScanExpired(j, now));
    if (expired.length === 0) return;
    const ids = new Set(expired.map((j) => j.id));
    setJobsAndRef((prev) => prev.filter((j) => !ids.has(j.id)));
    // Save the pruned list first, then delete the images, so a kill in
    // between can't leave a saved job whose image is gone. If the save fails
    // the images stay; the next launch's load drops the jobs and sweeps them.
    void (async () => {
      try {
        await saveJobs(jobsRef.current);
      } catch {
        return;
      }
      for (const j of expired) if (j.imageUri) void deleteScanImage(j.imageUri);
    })();
  }, [setJobsAndRef]);

  const queueForLater = useCallback(
    (jobId: string) => {
      updateJob(jobId, { status: 'queued' });
    },
    [updateJob]
  );

  const reviewJob = useCallback((jobId: string) => {
    setReviewingJobId(jobId);
  }, []);

  const dismissJob = useCallback(
    (jobId: string) => {
      // Read the latest jobs and review id, not this render's: runScan calls
      // this from a long-lived closure, which would otherwise see a stale
      // snapshot and skip deleting the photo (#588).
      const job = jobsRef.current.find((j) => j.id === jobId);
      setJobsAndRef((prev) => prev.filter((j) => j.id !== jobId));
      setReviewingJobId((current) => (current === jobId ? null : current));

      // Clean up persisted image if it exists.
      if (job?.imageUri) deleteScanImage(job.imageUri);
    },
    [setJobsAndRef]
  );

  // Closing the review is the user's "done with this photo": a completed job
  // has no other way back into view, so discard it (and free its image,
  // which was only kept around in case they wanted an enhanced re-scan).
  const dismissReview = useCallback(() => {
    const job = reviewingJobId ? jobsRef.current.find((j) => j.id === reviewingJobId) : undefined;
    if (job?.status === 'complete') {
      dismissJob(job.id);
    }
    setReviewingJobId(null);
  }, [reviewingJobId, dismissJob]);

  const requestEnhancedScan = useCallback(
    async (jobId: string) => {
      const job = jobsRef.current.find((j) => j.id === jobId);
      if (!job || job.type !== 'image' || !job.imageUri) return;

      Sentry.addBreadcrumb({
        category: 'scan',
        message: 'Enhanced scan requested',
        level: 'info',
        data: { jobId, hadResults: (job.results?.length ?? 0) > 0 },
      });

      const updated: ScanJob = { ...job, enhanced: true, status: 'pending', error: undefined };
      setJobsAndRef((prev) => prev.map((j) => (j.id === jobId ? updated : j)));
      setReviewingJobId(null);
      await executeScanRef.current(updated);
    },
    [setJobsAndRef]
  );

  // Keep the review id readable from long-lived closures (the banner action).
  useEffect(() => {
    reviewingJobIdRef.current = reviewingJobId;
  }, [reviewingJobId]);

  // "Add it yourself" (#616). One sheet at a time: close the review picker
  // WITHOUT dismissing its job (dismissReview would delete the whole job and
  // photo). A book leaves the job only once its form is saved, so cancelling
  // the form brings the review back with that book still in it.
  const addYourself = useCallback((books: EnrichedBook[]) => {
    if (books.length === 0) return;
    const jobId = reviewingJobIdRef.current;
    const job = jobId ? jobsRef.current.find((j) => j.id === jobId) : undefined;
    setReviewingJobId(null);
    reopenReviewJobIdRef.current = job ? job.id : null;
    setUnverifiedDrafts(
      books.map((b) => {
        nextDraftIdRef.current += 1;
        const id = `draft-${nextDraftIdRef.current}`;
        draftSourcesRef.current.set(id, { jobId: job ? job.id : null, key: candidateKey(b) });
        return { ...draftFromBook(b), id };
      })
    );
  }, []);

  // When the forms are done, show the review again if the job still has
  // books; a job whose every book was saved this way is finished.
  useEffect(() => {
    if (unverifiedDrafts.length > 0) return;
    const jobId = reopenReviewJobIdRef.current;
    if (!jobId) return;
    reopenReviewJobIdRef.current = null;
    const job = jobsRef.current.find((j) => j.id === jobId);
    if (!job) return;
    if ((job.results?.length ?? 0) > 0) setReviewingJobId(jobId);
    else if (job.status === 'complete') dismissJob(jobId);
  }, [unverifiedDrafts, dismissJob]);

  const dismissUnverified = useCallback(() => setUnverifiedDrafts([]), []);

  const unverifiedDraftsRef = useRef(unverifiedDrafts);
  useEffect(() => {
    unverifiedDraftsRef.current = unverifiedDrafts;
  }, [unverifiedDrafts]);

  const unverifiedSaved = useCallback(
    (title: string, draftId?: string) => {
      const id = draftId ?? unverifiedDraftsRef.current[0]?.id;
      const source = id ? draftSourcesRef.current.get(id) : undefined;
      if (id) draftSourcesRef.current.delete(id);
      const job = source?.jobId ? jobsRef.current.find((j) => j.id === source.jobId) : undefined;
      if (job && source) {
        updateJob(job.id, {
          results: (job.results ?? []).filter((b) => candidateKey(b) !== source.key),
        });
      }
      // Only that draft leaves the queue; a stale save leaves others alone.
      setUnverifiedDrafts((prev) => prev.filter((d) => d.id !== id));
      showBanner({
        message: t('unverifiedSavedMessage', { title }),
        type: 'success',
        duration: 4000,
      });
    },
    [showBanner, t, updateJob]
  );

  const handleAddBooks = useCallback(
    async (books: EnrichedBook[]) => {
      if (books.length === 0) return;
      const jobId = reviewingJobId;

      Sentry.addBreadcrumb({
        category: 'scan',
        message: `Adding ${books.length} book(s) from review`,
        level: 'info',
        data: { count: books.length },
      });

      // Adding to the wishlist is a server-authoritative write — never
      // attempt it offline. isConnected === false means "known offline";
      // null/undefined ("unknown", e.g. an unusual connection type) is
      // treated as connected, matching useNetworkStatus elsewhere.
      const netState = await NetInfo.fetch();
      if (netState.isConnected === false) {
        showBanner({
          message: t('requiresConnection', { ns: 'common' }),
          type: 'error',
          duration: 4000,
        });
        return;
      }

      const outcomes = await Promise.allSettled(books.map((book) => api.post('/wishlist', book)));
      // A 409 with an ownership code means the book is already in the
      // library (#587): nothing to retry and not a failure.
      const alreadyOwned = (o: PromiseSettledResult<unknown>) =>
        o.status === 'rejected' && isAlreadyOwnedError(o.reason);
      const owned = books.filter((_, i) => alreadyOwned(outcomes[i]));
      // A 422 catalog refusal (#612) is final: retrying cannot help, so the
      // book is "not saved" but neither retried nor reported to Sentry.
      const refusals = outcomes.map((o) =>
        o.status === 'rejected' ? catalogRefusal(o.reason) : null
      );
      const notInCatalog = books.filter((_, i) => refusals[i] === 'book_not_in_catalog');
      const staleResult = books.filter((_, i) => refusals[i] === 'catalog_token_invalid');
      const refused = notInCatalog.length + staleResult.length;
      const failed = books.filter(
        (_, i) => outcomes[i].status === 'rejected' && !alreadyOwned(outcomes[i])
      );
      // Failures a retry might fix.
      const retryable = books.filter(
        (_, i) =>
          outcomes[i].status === 'rejected' && !alreadyOwned(outcomes[i]) && refusals[i] === null
      );
      const newlyAdded = books.length - failed.length - owned.length;

      if (failed.length === 0) {
        if (jobId) dismissJob(jobId);
        setReviewingJobId(null);
        let message: string;
        if (owned.length === 0) {
          message =
            books.length === 1
              ? t('addedMessage', { title: books[0].title })
              : t('addedManyMessage', { count: books.length });
        } else if (newlyAdded === 0) {
          message =
            owned.length === 1
              ? t('alreadyInLibraryMessage', { title: owned[0].title })
              : t('alreadyInLibraryManyMessage', { count: owned.length });
        } else {
          message = t('addedAndOwnedMessage', { added: newlyAdded, owned: owned.length });
        }
        showBanner({
          message,
          type: newlyAdded === 0 ? 'info' : 'success',
          duration: 4000,
        });
        return;
      }

      for (const outcome of outcomes) {
        if (
          outcome.status === 'rejected' &&
          !alreadyOwned(outcome) &&
          catalogRefusal(outcome.reason) === null &&
          !isReportedCentrally(outcome.reason)
        ) {
          Sentry.captureException(sanitizeApiError(outcome.reason), {
            tags: { feature: 'scan', action: 'add_books', ...apiErrorTags(outcome.reason) },
          });
        }
      }
      // Keep only the books a retry could save; owned and stale books are
      // done with. While the picker stays open (something is retryable) a
      // book no catalog matched stays too, so its "Add it yourself" button
      // remains reachable (#616).
      const kept = books.filter((b) => retryable.includes(b) || notInCatalog.includes(b));
      if (jobId && retryable.length < books.length) {
        if (retryable.length === 0) {
          dismissJob(jobId);
          setReviewingJobId(null);
        } else {
          updateJob(jobId, { results: kept });
        }
      }
      // Count only the books actually added; say how many were already
      // in the library rather than counting them as added (#587).
      let message: string;
      if (refused > 0) {
        // Not saved, with the specific reason for the refused books (#612).
        if (
          newlyAdded === 0 &&
          owned.length === 0 &&
          retryable.length === 0 &&
          staleResult.length === 0
        ) {
          message =
            notInCatalog.length === 1
              ? t('notInCatalogMessage')
              : t('notInCatalogManyMessage', { count: notInCatalog.length });
        } else if (
          newlyAdded === 0 &&
          owned.length === 0 &&
          retryable.length === 0 &&
          notInCatalog.length === 0
        ) {
          message =
            staleResult.length === 1
              ? t('catalogTokenInvalidMessage')
              : t('catalogTokenInvalidManyMessage', { count: staleResult.length });
        } else {
          const counts = { added: newlyAdded, owned: owned.length, notSaved: failed.length };
          const summary =
            newlyAdded > 0
              ? owned.length > 0
                ? t('notSavedSummaryOwnedMessage', counts)
                : t('notSavedSummaryMessage', counts)
              : owned.length > 0
                ? t('notSavedOwnedOnlyMessage', counts)
                : t('notSavedOnlyMessage', counts);
          message = [
            summary,
            notInCatalog.length > 0 ? t('reasonNotInCatalog', { count: notInCatalog.length }) : '',
            staleResult.length > 0 ? t('reasonTokenInvalid', { count: staleResult.length }) : '',
            retryable.length > 0 ? t('retryOthersHint') : '',
          ]
            .filter(Boolean)
            .join(' ');
        }
      } else if (owned.length > 0) {
        message =
          newlyAdded > 0
            ? t('addedSomeOwnedMessage', {
                added: newlyAdded,
                owned: owned.length,
                failed: failed.length,
              })
            : t('ownedNotSavedRetryMessage', { owned: owned.length, failed: failed.length });
      } else if (newlyAdded > 0) {
        message = t('addedSomeMessage', { added: newlyAdded, total: books.length });
      } else {
        message = t('couldNotSaveMessage');
      }
      // Long, multi-sentence results are shown in full and stay long enough to read.
      showBanner({
        message,
        type: 'error',
        duration: Math.min(12000, Math.max(6000, message.length * 60)),
        numberOfLines: 0,
        // Books no catalog matched can still be added as the user's own
        // unverified entries (#616).
        actions:
          notInCatalog.length > 0
            ? [{ label: t('addItYourself'), onPress: () => addYourself(notInCatalog) }]
            : undefined,
      });
    },
    [reviewingJobId, dismissJob, updateJob, showBanner, t, addYourself]
  );

  // Keep executeScan in a ref so useCallbacks always call the latest version
  // without creating circular deps. Updated after every render so closures
  // always capture the current retryScan / queueForLater callbacks.
  useEffect(() => {
    // The review sheet closes when an enhanced re-scan starts, and no screen
    // lists jobs, so reopen it on the earlier results or they'd be unreachable.
    function restorePriorResults(
      job: ScanJob,
      updates: Partial<ScanJob>,
      message: string | null,
      type: 'info' | 'error'
    ) {
      updateJob(job.id, { status: 'complete', error: undefined, ...updates });
      setReviewingJobId(job.id);
      if (message) showBanner({ message, type, duration: 6000 });
    }

    // A refusal from POST /scan (limit, busy, paused). Not a failure worth a
    // Sentry event or a retry prompt: the screen shows a notice and the job
    // stops (the drain halts at the first refusal, so it can't spam the API).
    function handleScanRefusal(job: ScanJob, state: ScanUiState) {
      const notice = toScanNotice(state, Date.now(), true);
      if (!notice) return;
      // A refusal applies to every queued photo (and a busy lease means the
      // next one would be refused too), so stop sending photos; an
      // enhanced-only switch-off stops enhanced photos only.
      const scope = state.kind === 'paused' && state.enhancedOnly ? 'enhanced' : 'images';
      if (stopDrainRef.current !== 'images') stopDrainRef.current = scope;
      Sentry.addBreadcrumb({
        category: 'scan',
        message: `Scan refused: ${state.kind}`,
        level: 'info',
        data: { jobId: job.id },
      });
      // A refused enhanced re-scan must not strand the standard scan's books.
      // The notice card speaks, so no banner with the same text.
      if (job.enhanced && job.results?.length) {
        setScanNotice(notice);
        restorePriorResults(job, { enhanced: false }, null, 'info');
        return;
      }
      setScanNotice(notice);
      // A refusal isn't a failure: the photo goes back in the queue (so it is
      // counted by the pending-uploads badge, survives a restart and is sent
      // by the next drain or by "Try again"). It is not retried within this
      // drain, which stops here, so a refusal costs at most one request.
      // A refusal ends any retry schedule: the photo starts again at attempt 1.
      updateJob(job.id, {
        status: 'queued',
        error: state.kind,
        retryCount: 0,
        attempts: undefined,
        nextAttemptAt: undefined,
      });
    }

    // A retryable outcome (#604: 503 scan_unavailable, the provider was out
    // or sent a malformed reply; refunded server-side). The photo waits for
    // its next attempt on SCAN_RETRY_DELAYS_MS, and the drain sends no other
    // photo until then. After MAX_SCAN_ATTEMPTS it is failed.
    function handleUnavailable(
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
        const otherFirstRetryPending = jobsRef.current.some(
          (j) => j.id !== job.id && isAwaitingRetry(j) && j.attempts === 1
        );
        if (!otherFirstRetryPending) setScanNotice((n) => (n?.kind === 'paused' ? null : n));
      };
      if (attempt >= MAX_SCAN_ATTEMPTS) {
        updateJob(job.id, {
          status: 'failed',
          error: 'unavailable',
          attempts: attempt,
          nextAttemptAt: undefined,
        });
        clearRetryNotice();
        // The photo no longer holds the queue: the drain sending it (which
        // never stopped, see below) goes on with the rest, and if none is
        // running one starts.
        const othersWaiting = jobsRef.current.some(
          (j) => j.id !== job.id && isPendingUpload(j) && !inFlightRef.current.has(j.id)
        );
        if (othersWaiting && !drainingRef.current) void drainQueue();
        // No image, URI or user content: just the counts and the code.
        Sentry.captureMessage('Scan failed after all retries', {
          level: 'warning',
          fingerprint: ['scan-final-failure'],
          tags: { feature: 'scan', attempts: String(attempt), last_error: state.code },
        });
        showBanner({
          message: t('scanFailedFinal'),
          type: 'error',
          duration: 8000,
          actions: [
            { label: t('scanFailedFinalRetry'), onPress: () => restartScanSchedule(job.id) },
            { label: t('scanFailedFinalViewQueue'), onPress: openScanQueue },
          ],
        });
        return;
      }
      // The photo holds the queue until its next attempt: this drain sends no
      // more photos (text searches still go).
      stopDrainRef.current = 'images';
      updateJob(job.id, {
        status: 'queued',
        error: 'unavailable',
        retryCount: 0,
        attempts: attempt,
        nextAttemptAt: Date.now() + SCAN_RETRY_DELAYS_MS[attempt - 1],
      });
      if (attempt === 1) {
        const notice = toScanNotice(state);
        if (notice) setScanNotice(notice);
      } else {
        clearRetryNotice();
      }
    }

    // An enhanced scan the server refused (#533: not Premium, or throttled
    // after repeated empty enhanced scans). Only
    // enhanced photos are affected: the drain stops sending enhanced photos
    // (standard ones still go), the offer is hidden until the server reports
    // enhanced scans left again, and the standard scan's books come back.
    function handleEnhancedRefusal(
      job: ScanJob,
      state: Extract<ScanUiState, { kind: 'enhanced_refused' }>
    ) {
      if (stopDrainRef.current === 'none') stopDrainRef.current = 'enhanced';
      setEnhancedScansRemaining(0);
      Sentry.addBreadcrumb({
        category: 'scan',
        message: `Enhanced scan refused: ${state.reason}`,
        level: 'info',
        data: { jobId: job.id },
      });
      const message = enhancedRefusalMessage(state, t, i18n.language);
      // Fall back to whatever the standard scan found, if anything.
      if (job.results?.length) {
        restorePriorResults(job, { enhanced: false }, message, 'info');
      } else {
        updateJob(job.id, { status: 'failed', enhanced: false, error: 'no_results' });
        showBanner({ message, type: 'info', duration: 6000 });
      }
    }

    executeScanRef.current = async function executeScan(job: ScanJob) {
      // Synchronous (before any await) so a drain can't start the same job.
      inFlightRef.current.add(job.id);
      try {
        await runScan(job);
      } finally {
        inFlightRef.current.delete(job.id);
        // A retry queued behind this send (outside a drain; a drain handles
        // it when it ends). Left for a photo still being sent, so a text
        // search finishing first doesn't send the retry alongside it.
        if (drainAgainRef.current && !drainingRef.current && !imageInFlight()) {
          drainAgainRef.current = false;
          void drainQueue();
        }
      }
    };

    async function runScan(job: ScanJob) {
      updateJob(job.id, { status: 'searching' });
      // Enhanced scans left as reported by this response (the state update
      // lands next render).
      let reportedRemaining: number | null = enhancedScansRemaining;
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
          });
          results = response.data ?? [];
        } else {
          const formData = new FormData();
          if (job.enhanced) formData.append('enhanced', 'true');
          // The server accepts 1-6 (#603) and ignores it until that ships.
          formData.append('attempt', String(attempt));
          if (Platform.OS === 'web') {
            formData.append('file', job.imageUri as unknown as Blob, 'scan.jpg');
          } else {
            formData.append('file', {
              uri: job.imageUri,
              name: 'scan.jpg',
              type: 'image/jpeg',
            } as unknown as Blob);
          }
          const postScan = () =>
            api.post<ScanResponse>('/scan', formData, {
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              transformRequest: [
                (data: FormData, headers: any) => {
                  if (Platform.OS === 'web') {
                    headers.delete('Content-Type');
                  } else {
                    headers.set('Content-Type', 'multipart/form-data');
                  }
                  return data;
                },
              ],
            });
          // 429 scan_in_progress means another scan holds the user's lease:
          // wait Retry-After and retry silently, at most MAX_IN_PROGRESS_RETRIES
          // times. If it persists the error propagates and the catch below
          // shows the neutral "Still processing" notice.
          let response!: Awaited<ReturnType<typeof postScan>>;
          for (let attempt = 0, done = false; !done; attempt++) {
            try {
              response = await postScan();
              done = true;
            } catch (postErr) {
              const state = classifyScanError(postErr);
              if (state.kind !== 'in_progress' || attempt >= MAX_IN_PROGRESS_RETRIES) {
                throw postErr;
              }
              await sleep(state.retryAfterSeconds * 1000);
            }
          }
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
          if (jobsRef.current.some(pullForward)) {
            setJobsAndRef((list) =>
              list.map((j) => (pullForward(j) ? { ...j, nextAttemptAt: now } : j))
            );
          }
          const waiting = jobsRef.current.some(
            (j) =>
              j.id !== job.id &&
              (j.status === 'queued' || j.status === 'pending') &&
              !inFlightRef.current.has(j.id)
          );
          if (waiting && !drainingRef.current) void drainQueue();
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
            setEnhancedScansRemaining(remaining);
          }
        }

        if (results.length === 0) {
          // An enhanced scan that found nothing leaves the earlier results intact.
          if (job.enhanced && job.results?.length) {
            restorePriorResults(job, { enhanced: true }, t('enhancedNoNewBooks'), 'info');
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
              ? [{ label: t('tryEnhanced'), onPress: () => requestEnhancedScan(job.id) }]
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
          setJobsAndRef((prev) => prev.filter((j) => j.id !== job.id));
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
        // A status-0 / no-response error almost always means the OS suspended
        // the upload mid-flight (app backgrounded). Auto-queue so it drains
        // on return instead of asking the user to manually retry. (#213)
        const isNetworkSuspend =
          err != null &&
          typeof err === 'object' &&
          'response' in err &&
          (err as { response: unknown }).response == null;

        if (isNetworkSuspend) {
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

        const uiState =
          job.type === 'image' ? classifyScanError(err) : { kind: 'generic' as const };

        // The server answered, so a scan may have been counted (or refunded):
        // refresh the counters. Not for paused/in_progress (nothing reserved),
        // and not for network errors, which returned above.
        if (
          job.type === 'image' &&
          (err as { response?: unknown } | null)?.response != null &&
          uiState.kind !== 'paused' &&
          // #604: refunded server-side.
          uiState.kind !== 'unavailable' &&
          uiState.kind !== 'in_progress' &&
          uiState.kind !== 'enhanced_refused' &&
          // #586: refused before anything was taken; the counters are unchanged.
          uiState.kind !== 'requests_daily_limit'
        ) {
          bumpUsage();
        }

        if (
          uiState.kind === 'daily_limit' ||
          uiState.kind === 'requests_daily_limit' ||
          uiState.kind === 'in_progress' ||
          uiState.kind === 'paused'
        ) {
          handleScanRefusal(job, uiState);
          return;
        }

        if (uiState.kind === 'unavailable') {
          // An enhanced re-scan is never retried: like before #604 it takes
          // the paused path (its earlier books back if it has any, otherwise
          // queued with the paused notice). No schedule, banner or Sentry event.
          if (job.enhanced) {
            handleScanRefusal(job, { kind: 'paused' });
          } else {
            handleUnavailable(job, attempt, uiState);
          }
          return;
        }

        if (job.enhanced && uiState.kind === 'enhanced_refused') {
          handleEnhancedRefusal(job, uiState);
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
          restorePriorResults(job, { enhanced: false }, t('enhancedFailed'), 'error');
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
            { label: t('retryNow'), onPress: () => retryScan(job.id) },
            { label: t('saveForLater'), onPress: () => queueForLater(job.id) },
          ],
          duration: 8000,
        });
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    showBanner,
    t,
    i18n,
    bumpUsage,
    retryScan,
    queueForLater,
    requestEnhancedScan,
    restartScanSchedule,
    enhancedScansRemaining,
  ]);

  const reviewingJob = useMemo(
    () => (reviewingJobId ? (jobs.find((j) => j.id === reviewingJobId) ?? null) : null),
    [reviewingJobId, jobs]
  );

  const value = useMemo(
    () => ({
      jobs,
      pendingCount,
      isQueueFull,
      reviewingJob,
      enhancedScansRemaining,
      scanNotice,
      dismissScanNotice,
      resumeQueue,
      usageRefreshKey,
      startScan,
      retryScan,
      queueForLater,
      reviewJob,
      dismissReview,
      dismissJob,
      restartScanSchedule,
      purgeExpiredFailedJobs,
      requestEnhancedScan,
      handleAddBooks,
      unverifiedDrafts,
      addYourself,
      unverifiedSaved,
      dismissUnverified,
    }),
    [
      jobs,
      pendingCount,
      isQueueFull,
      reviewingJob,
      enhancedScansRemaining,
      scanNotice,
      dismissScanNotice,
      resumeQueue,
      usageRefreshKey,
      startScan,
      retryScan,
      queueForLater,
      reviewJob,
      dismissReview,
      dismissJob,
      restartScanSchedule,
      purgeExpiredFailedJobs,
      requestEnhancedScan,
      handleAddBooks,
      unverifiedDrafts,
      addYourself,
      unverifiedSaved,
      dismissUnverified,
    ]
  );

  return <ScanJobContext.Provider value={value}>{children}</ScanJobContext.Provider>;
}
