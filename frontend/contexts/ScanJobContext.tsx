import React, { createContext, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Platform } from 'react-native';
import { useTranslation } from 'react-i18next';

import type { EnrichedBook } from '../components/BookCandidatePicker';
import { useAuth } from '../hooks/useAuth';
import { useBanner } from '../hooks/useBanner';
import { useScanQueue } from '../hooks/useScanQueue';
import { useScanReview } from '../hooks/useScanReview';
import { useTurnstile } from '../hooks/useTurnstile';
import { Sentry } from '../lib/sentry';
import { MAX_SCAN_RETRY_DELAY_MS, type ScanNotice } from '../lib/scanErrors';
import {
  isFailedScanExpired,
  isPendingUpload,
  MAX_QUEUE_SIZE,
  type ScanJob,
  type ScanJobType,
} from '../lib/scanJob';
import {
  clearJobs,
  deleteScanImage,
  loadJobs,
  saveJobs,
  sweepOrphanedScanFiles,
} from '../lib/scanJobStorage';
import { executeScan } from '../lib/scan/executeScan';
import type { ScanExecDeps } from '../lib/scan/types';
import type { UnverifiedDraft } from '../lib/unverified';

/** An "Add it yourself" form waiting to be shown, with its own id (#616). */
export type PendingUnverifiedDraft = UnverifiedDraft & { id: string };

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
  const executeScanRef = useRef<(job: ScanJob) => Promise<void>>(async () => {});
  // Set when the initial load fails, so the persist effect below doesn't
  // immediately overwrite storage with the empty `jobs` state it starts
  // with — we don't know what was actually stored. Consumed on first fire.
  const skipNextPersistRef = useRef(false);
  const { showBanner } = useBanner();
  const { t, i18n } = useTranslation('scan');
  const { isAuthenticated } = useAuth();
  // A fresh Turnstile token per POST /scan (#740). Read through a ref so the
  // long-lived executeScan closure always uses the current provider.
  const { getToken: getTurnstileToken, configured: turnstileConfigured } = useTurnstile();
  const getTurnstileTokenRef = useRef(getTurnstileToken);
  const turnstileConfiguredRef = useRef(turnstileConfigured);
  useEffect(() => {
    getTurnstileTokenRef.current = getTurnstileToken;
    turnstileConfiguredRef.current = turnstileConfigured;
  }, [getTurnstileToken, turnstileConfigured]);
  // Bumped on sign-out. A send started under an earlier generation drops its
  // result instead of writing it into the next account's queue.
  const generationRef = useRef(0);
  const wasAuthenticatedRef = useRef(false);
  // Requests in flight (scans, searches, wishlist adds); aborted on sign-out.
  const abortersRef = useRef(new Set<AbortController>());

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

  // Sign-out (the Settings button or a revoked session): the queue belongs to
  // the account that made it, so drop it before another account signs in.
  // Only a true -> false change counts; the launch state never clears.
  useEffect(() => {
    const wasAuthenticated = wasAuthenticatedRef.current;
    wasAuthenticatedRef.current = isAuthenticated;
    if (!wasAuthenticated || isAuthenticated) return;
    // Taken now, so a photo captured after the next sign-in is not swept.
    const signedOutAt = Date.now();
    generationRef.current += 1;
    drainAgainRef.current = false;
    for (const c of abortersRef.current) c.abort();
    abortersRef.current.clear();
    draftSourcesRef.current.clear();
    reopenReviewJobIdRef.current = null;
    const dropped = jobsRef.current;
    // Emptying the list also disarms the retry timer and starves any drain.
    setJobsAndRef(() => []);
    setReviewingJobId(null);
    reviewingJobIdRef.current = null;
    setScanNotice(null);
    setUnverifiedDrafts([]);
    setEnhancedScansRemaining(null);
    void (async () => {
      try {
        await clearJobs();
      } catch {
        // Best-effort; the next sign-in's load would otherwise see the old queue.
      }
      for (const j of dropped) {
        if (!j.imageUri) continue;
        // Web photos are blob URLs held in memory.
        if (Platform.OS === 'web' && j.imageUri.startsWith('blob:')) {
          try {
            URL.revokeObjectURL(j.imageUri);
          } catch {
            // Best-effort.
          }
        }
        await deleteScanImage(j.imageUri);
      }
      await sweepOrphanedScanFiles([], signedOutAt);
    })();
  }, [isAuthenticated, setJobsAndRef]);

  // Load persisted jobs on mount. Reset interrupted searches to pending, then
  // remove scan-queue files that no persisted job refers to (crashed captures).
  useEffect(() => {
    (async () => {
      const launchedAt = Date.now();
      const loadGeneration = generationRef.current;
      const persisted = await loadJobs();
      // Signed out while the read was pending: it belongs to the old account.
      if (generationRef.current !== loadGeneration) {
        skipNextPersistRef.current = false;
        setLoaded(true);
        return;
      }
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

  // The queue: drain, reconnect drain, retry timer, and the send entry
  // points (hooks/useScanQueue.ts).
  const { drainQueue, resumeQueue, startScan, retryScan, restartScanSchedule } = useScanQueue({
    jobs,
    loaded,
    jobsRef,
    setJobsAndRef,
    updateJob,
    inFlightRef,
    generationRef,
    drainingRef,
    drainAgainRef,
    stopDrainRef,
    imageInFlight,
    setScanNotice,
    showBanner,
    t,
    executeScanRef,
  });

  const pendingCount = useMemo(() => jobs.filter(isPendingUpload).length, [jobs]);
  const isQueueFull = pendingCount >= MAX_QUEUE_SIZE;

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

  // Add-books and the "Add it yourself" drafts (hooks/useScanReview.ts).
  const { handleAddBooks, addYourself, unverifiedSaved, dismissUnverified } = useScanReview({
    jobsRef,
    updateJob,
    generationRef,
    abortersRef,
    showBanner,
    t,
    reviewingJobId,
    setReviewingJobId,
    reviewingJobIdRef,
    dismissJob,
    unverifiedDrafts,
    setUnverifiedDrafts,
    reopenReviewJobIdRef,
    draftSourcesRef,
  });

  // Keep executeScan in a ref so useCallbacks always call the latest version
  // without creating circular deps. Updated after every render so closures
  // always capture the current retryScan / queueForLater callbacks.
  useEffect(() => {
    const deps: ScanExecDeps = {
      updateJob,
      setJobsAndRef,
      setReviewingJobId,
      setScanNotice,
      setEnhancedScansRemaining,
      showBanner,
      bumpUsage,
      drainQueue,
      imageInFlight,
      retryScan,
      queueForLater,
      requestEnhancedScan,
      restartScanSchedule,
      enhancedScansRemaining,
      t,
      i18n,
      jobsRef,
      inFlightRef,
      abortersRef,
      generationRef,
      drainingRef,
      drainAgainRef,
      stopDrainRef,
      getTurnstileTokenRef,
      turnstileConfiguredRef,
    };
    executeScanRef.current = (job: ScanJob) => executeScan(deps, job);
    // updateJob, setJobsAndRef, drainQueue and imageInFlight never change, so
    // listing them re-runs this no more often than the values below.
  }, [
    updateJob,
    setJobsAndRef,
    drainQueue,
    imageInFlight,
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
