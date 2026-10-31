import { useCallback, useEffect, useMemo, type MutableRefObject } from 'react';
import NetInfo from '@react-native-community/netinfo';
import * as Crypto from 'expo-crypto';

import { isReportedCentrally, sanitizeApiError } from '../lib/apiErrorReporting';
import { Sentry } from '../lib/sentry';
import type { ScanExecDeps } from '../lib/scan/types';
import { hasRefusedJobs, MAX_SCAN_RETRY_DELAY_MS } from '../lib/scanErrors';
import {
  isAwaitingRetry,
  isPendingUpload,
  MAX_QUEUE_SIZE,
  type ScanJob,
  type ScanJobType,
} from '../lib/scanJob';
import { deleteScanImage } from '../lib/scanJobStorage';

const MAX_RETRIES = 3;
const QUEUE_DRAIN_DELAY = 2000;
// Extra drain passes when a retry wait ends mid-drain (#604).
const MAX_DRAIN_PASSES = 3;
// When the retry timer fires during a drain, check again after this long.
const RETRY_TIMER_BUSY_DELAY = 1000;
// When the retry timer fires while offline, check again after this long (#604).
const RETRY_TIMER_OFFLINE_DELAY = 60 * 1000;

/**
 * What the queue needs from ScanJobProvider. The refs are the provider's own
 * (shared with executeScan and the sign-out effect) and are read at call time.
 */
export type ScanQueueDeps = Pick<
  ScanExecDeps,
  | 'jobsRef'
  | 'setJobsAndRef'
  | 'updateJob'
  | 'inFlightRef'
  | 'generationRef'
  | 'drainingRef'
  | 'drainAgainRef'
  | 'stopDrainRef'
  | 'imageInFlight'
  | 'setScanNotice'
  | 'showBanner'
  | 't'
> & {
  jobs: ScanJob[];
  loaded: boolean;
  /** The latest executeScan, set by the provider; read at call time. */
  executeScanRef: MutableRefObject<(job: ScanJob) => Promise<void>>;
};

/**
 * The scan queue (#604, #605): the drain (one at a time, `drainingRef` /
 * `drainAgainRef`), the reconnect drain, the retry-schedule timer and the
 * entry points that send a scan now or hand it to the drain.
 */
export function useScanQueue({
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
}: ScanQueueDeps) {
  // Whether a photo is being uploaded right now (past its connectivity
  // check), which a drain must not send another photo alongside.
  const imageUploading = useCallback(
    () =>
      [...inFlightRef.current].some((id) => {
        const j = jobsRef.current.find((x) => x.id === id);
        return j?.type === 'image' && j.status === 'searching';
      }),
    [inFlightRef, jobsRef]
  );

  // Drain queued/pending jobs. Only uses refs and stable callbacks, so it is
  // created once and every caller (timer, listener, executeScan) shares it.
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
  }, [
    drainAgainRef,
    drainingRef,
    executeScanRef,
    imageUploading,
    inFlightRef,
    jobsRef,
    stopDrainRef,
  ]);

  const resumeQueue = useCallback(() => {
    setScanNotice(null);
    if (!drainingRef.current) void drainQueue();
  }, [drainQueue, drainingRef, setScanNotice]);

  // Listen for connectivity changes and drain queued jobs. Re-subscribed
  // only when `loaded` changes (drainQueue and the ref never change).
  useEffect(() => {
    const unsubscribe = NetInfo.addEventListener((state) => {
      if (state.isConnected && !drainingRef.current) {
        drainQueue();
      }
    });
    return unsubscribe;
  }, [loaded, drainQueue, drainingRef]);

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
  }, [loaded, nextRetryAt, drainQueue, drainingRef]);

  const startScan = useCallback(
    async (type: ScanJobType, imageUri?: string, query?: string) => {
      let startedId: string | null = null;
      const generation = generationRef.current;
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
        // Signed out during the connectivity check: the job was dropped with
        // the queue, so nothing is sent or announced.
        if (generationRef.current !== generation) return;
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
    [showBanner, t, generationRef, jobsRef, setJobsAndRef, inFlightRef, updateJob, executeScanRef]
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
    [showBanner, t, jobsRef, updateJob, setJobsAndRef, executeScanRef]
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
    [
      setJobsAndRef,
      drainQueue,
      imageInFlight,
      jobsRef,
      inFlightRef,
      drainingRef,
      drainAgainRef,
      executeScanRef,
    ]
  );

  return { drainQueue, resumeQueue, startScan, retryScan, restartScanSchedule };
}
