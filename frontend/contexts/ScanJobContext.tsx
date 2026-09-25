import React, { createContext, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Platform } from 'react-native';
import NetInfo from '@react-native-community/netinfo';
import * as Crypto from 'expo-crypto';
import { useTranslation } from 'react-i18next';

import type { EnrichedBook } from '../components/BookCandidatePicker';
import { useBanner } from '../hooks/useBanner';
import { api } from '../lib/api';
import { Sentry } from '../lib/sentry';
import {
  isPendingUpload,
  MAX_QUEUE_SIZE,
  type ScanJob,
  type ScanJobType,
  type ScanResponse,
} from '../lib/scanJob';
import { deleteScanImage, loadJobs, saveJobs, sweepOrphanedScanFiles } from '../lib/scanJobStorage';

const MAX_RETRIES = 3;
const QUEUE_DRAIN_DELAY = 2000;

export interface ScanJobContextValue {
  jobs: ScanJob[];
  /** Scans waiting to upload (queued offline or pending). */
  pendingCount: number;
  /** True when the queue is at MAX_QUEUE_SIZE and no more captures can be accepted. */
  isQueueFull: boolean;
  reviewingJob: ScanJob | null;
  /** Enhanced-scan credits left, as last reported by the server; null until a scan reports it. */
  enhancedCredits: number | null;
  startScan: (type: ScanJobType, imageUri?: string, query?: string) => void;
  retryScan: (jobId: string) => void;
  queueForLater: (jobId: string) => void;
  reviewJob: (jobId: string) => void;
  dismissReview: () => void;
  dismissJob: (jobId: string) => void;
  /** Re-run an image scan with the stronger model (costs one credit if it finds books). */
  requestEnhancedScan: (jobId: string) => void;
  /** Add the books ticked in the review checklist to the wishlist. */
  handleAddBooks: (books: EnrichedBook[]) => Promise<void>;
}

export const ScanJobContext = createContext<ScanJobContextValue>({
  jobs: [],
  pendingCount: 0,
  isQueueFull: false,
  reviewingJob: null,
  enhancedCredits: null,
  startScan: () => {},
  retryScan: () => {},
  queueForLater: () => {},
  reviewJob: () => {},
  dismissReview: () => {},
  dismissJob: () => {},
  requestEnhancedScan: () => {},
  handleAddBooks: async () => {},
});

/** True when the request failed because the user has no enhanced-scan credits. */
function isOutOfCredits(err: unknown): boolean {
  const response = (err as { response?: { status?: number } } | null)?.response;
  return response?.status === 402;
}

export function ScanJobProvider({ children }: { children: React.ReactNode }) {
  const [jobs, setJobs] = useState<ScanJob[]>([]);
  const [reviewingJobId, setReviewingJobId] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [enhancedCredits, setEnhancedCredits] = useState<number | null>(null);
  const drainingRef = useRef(false);
  const executeScanRef = useRef<(job: ScanJob) => Promise<void>>(async () => {});
  // Set when the initial load fails, so the persist effect below doesn't
  // immediately overwrite storage with the empty `jobs` state it starts
  // with — we don't know what was actually stored. Consumed on first fire.
  const skipNextPersistRef = useRef(false);
  const { showBanner } = useBanner();
  const { t } = useTranslation('scan');

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
      const restored = persisted
        .filter((j) => j.status !== 'complete')
        .map((j) => (j.status === 'searching' ? { ...j, status: 'pending' as const } : j));
      setJobs(restored);
      setLoaded(true);
      await sweepOrphanedScanFiles(restored, launchedAt);
    })();
  }, []);

  function updateJob(jobId: string, updates: Partial<ScanJob>) {
    setJobs((prev) => prev.map((j) => (j.id === jobId ? { ...j, ...updates } : j)));
  }

  // Drain queued/pending jobs when connectivity is restored. Declared before
  // the connectivity effect to satisfy no-use-before-define. Only uses stable
  // refs and setJobs so deps are empty.
  const drainQueue = useCallback(async () => {
    drainingRef.current = true;
    try {
      // Read the latest jobs from state via a callback to avoid stale closures.
      let queuedJobs: ScanJob[] = [];
      setJobs((prev) => {
        queuedJobs = prev.filter((j) => j.status === 'queued' || j.status === 'pending');
        return prev;
      });

      for (const job of queuedJobs) {
        const netState = await NetInfo.fetch();
        if (!netState.isConnected) break;
        await executeScanRef.current(job);
        // Delay between jobs to avoid API burst.
        if (queuedJobs.indexOf(job) < queuedJobs.length - 1) {
          await new Promise((r) => setTimeout(r, QUEUE_DRAIN_DELAY));
        }
      }
    } finally {
      drainingRef.current = false;
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

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

  // Keep jobs in a ref so callbacks can read the latest state synchronously.
  const jobsRef = useRef(jobs);
  useEffect(() => {
    jobsRef.current = jobs;
  }, [jobs]);

  const pendingCount = useMemo(() => jobs.filter(isPendingUpload).length, [jobs]);
  const isQueueFull = pendingCount >= MAX_QUEUE_SIZE;

  const startScan = useCallback(
    async (type: ScanJobType, imageUri?: string, query?: string) => {
      try {
        // Backstop for the cap — the scan screen checks isQueueFull before
        // persisting a capture, but a stale render could still get here.
        if (jobsRef.current.filter(isPendingUpload).length >= MAX_QUEUE_SIZE) {
          if (imageUri) deleteScanImage(imageUri);
          showBanner({ message: t('queueFull'), type: 'error', duration: 4000 });
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

        setJobs((prev) => [job, ...prev]);

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
        Sentry.captureException(err);
        showBanner({
          message: t('scanFailedMessage'),
          type: 'error',
          duration: 4000,
        });
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
      };
      setJobs((prev) => prev.map((j) => (j.id === jobId ? updated : j)));
      await executeScanRef.current(updated);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [showBanner, t]
  );

  const queueForLater = useCallback((jobId: string) => {
    updateJob(jobId, { status: 'queued' });
  }, []);

  const reviewJob = useCallback((jobId: string) => {
    setReviewingJobId(jobId);
  }, []);

  const dismissJob = useCallback(
    (jobId: string) => {
      setJobs((prev) => prev.filter((j) => j.id !== jobId));
      if (reviewingJobId === jobId) setReviewingJobId(null);

      // Clean up persisted image if it exists.
      const job = jobs.find((j) => j.id === jobId);
      if (job?.imageUri) deleteScanImage(job.imageUri);
    },
    [jobs, reviewingJobId]
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

  const requestEnhancedScan = useCallback(async (jobId: string) => {
    const job = jobsRef.current.find((j) => j.id === jobId);
    if (!job || job.type !== 'image' || !job.imageUri) return;

    Sentry.addBreadcrumb({
      category: 'scan',
      message: 'Enhanced scan requested',
      level: 'info',
      data: { jobId, hadResults: (job.results?.length ?? 0) > 0 },
    });

    const updated: ScanJob = { ...job, enhanced: true, status: 'pending', error: undefined };
    setJobs((prev) => prev.map((j) => (j.id === jobId ? updated : j)));
    setReviewingJobId(null);
    await executeScanRef.current(updated);
  }, []);

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
      const failed = books.filter((_, i) => outcomes[i].status === 'rejected');
      const addedCount = books.length - failed.length;

      if (failed.length === 0) {
        if (jobId) dismissJob(jobId);
        setReviewingJobId(null);
        showBanner({
          message:
            books.length === 1
              ? t('addedMessage', { title: books[0].title })
              : t('addedManyMessage', { count: books.length }),
          type: 'success',
          duration: 4000,
        });
        return;
      }

      for (const outcome of outcomes) {
        if (outcome.status === 'rejected') {
          Sentry.captureException(outcome.reason, {
            tags: { feature: 'scan', action: 'add_books' },
          });
        }
      }
      // Keep only the books that didn't save so the user can retry just those.
      if (jobId && addedCount > 0) updateJob(jobId, { results: failed });
      showBanner({
        message:
          addedCount > 0
            ? t('addedSomeMessage', { added: addedCount, total: books.length })
            : t('couldNotSaveMessage'),
        type: 'error',
        duration: 6000,
      });
    },
    [reviewingJobId, dismissJob, showBanner, t]
  );

  // Keep executeScan in a ref so useCallbacks always call the latest version
  // without creating circular deps. Updated after every render so closures
  // always capture the current retryScan / queueForLater callbacks.
  useEffect(() => {
    executeScanRef.current = async function executeScan(job: ScanJob) {
      updateJob(job.id, { status: 'searching' });
      // Credits reported by this response (the state update lands next render).
      let reportedCredits: number | null = enhancedCredits;

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
          if (Platform.OS === 'web') {
            formData.append('file', job.imageUri as unknown as Blob, 'scan.jpg');
          } else {
            formData.append('file', {
              uri: job.imageUri,
              name: 'scan.jpg',
              type: 'image/jpeg',
            } as unknown as Blob);
          }
          const response = await api.post<ScanResponse>('/scan', formData, {
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
          results = response.data?.books ?? [];
          const credits = response.data?.enhanced_scan_credits;
          if (typeof credits === 'number') {
            reportedCredits = credits;
            setEnhancedCredits(credits);
          }
        }

        if (results.length === 0) {
          // An enhanced scan that found nothing leaves any earlier results intact.
          if (job.enhanced && job.results?.length) {
            updateJob(job.id, { status: 'complete' });
          } else {
            updateJob(job.id, { status: 'failed', error: 'no_results' });
          }
          // Offer the stronger model once, unless this already was the enhanced
          // pass or the server told us the user is out of credits.
          const canEnhance = job.type === 'image' && !job.enhanced && reportedCredits !== 0;
          showBanner({
            message: job.enhanced ? t('enhancedNoBooksFound') : t('noBooksFoundTitle'),
            type: 'info',
            duration: canEnhance ? 8000 : 4000,
            actions: canEnhance
              ? [{ label: t('tryEnhanced'), onPress: () => requestEnhancedScan(job.id) }]
              : undefined,
          });
          return;
        }

        // Keep the image: the user may still ask for an enhanced re-scan from the
        // review sheet. It's deleted when the job is dismissed.
        updateJob(job.id, { status: 'complete', results, error: undefined });
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
          updateJob(job.id, { status: 'queued' });
          showBanner({
            message: t('savedOffline'),
            type: 'info',
            duration: 4000,
          });
          return;
        }

        if (job.enhanced && isOutOfCredits(err)) {
          setEnhancedCredits(0);
          // Fall back to whatever the standard scan found, if anything.
          updateJob(
            job.id,
            job.results?.length
              ? { status: 'complete', enhanced: false }
              : { status: 'failed', enhanced: false, error: 'no_results' }
          );
          showBanner({ message: t('noEnhancedCredits'), type: 'error', duration: 6000 });
          return;
        }

        Sentry.captureException(err, {
          tags: { feature: 'scan', action: 'execute_scan', jobType: job.type },
        });
        updateJob(job.id, { status: 'failed', error: 'network_or_server' });
        showBanner({
          message: t('scanFailedTitle'),
          type: 'error',
          actions: [
            { label: t('retryNow'), onPress: () => retryScan(job.id) },
            { label: t('saveForLater'), onPress: () => queueForLater(job.id) },
          ],
          duration: 8000,
        });
      }
    };
  }, [showBanner, t, retryScan, queueForLater, requestEnhancedScan, enhancedCredits]); // eslint-disable-line react-hooks/exhaustive-deps

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
      enhancedCredits,
      startScan,
      retryScan,
      queueForLater,
      reviewJob,
      dismissReview,
      dismissJob,
      requestEnhancedScan,
      handleAddBooks,
    }),
    [
      jobs,
      pendingCount,
      isQueueFull,
      reviewingJob,
      enhancedCredits,
      startScan,
      retryScan,
      queueForLater,
      reviewJob,
      dismissReview,
      dismissJob,
      requestEnhancedScan,
      handleAddBooks,
    ]
  );

  return <ScanJobContext.Provider value={value}>{children}</ScanJobContext.Provider>;
}
