import React, { createContext, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Platform } from 'react-native';
import NetInfo from '@react-native-community/netinfo';
import * as Crypto from 'expo-crypto';
import { useTranslation } from 'react-i18next';

import type { EnrichedBook } from '../components/BookCandidatePicker';
import { useBanner } from '../hooks/useBanner';
import { api } from '../lib/api';
import { Sentry } from '../lib/sentry';
import type { ScanJob, ScanJobType } from '../lib/scanJob';
import { loadJobs, saveJobs } from '../lib/scanJobStorage';

const MAX_RETRIES = 3;
const QUEUE_DRAIN_DELAY = 2000;

export interface ScanJobContextValue {
  jobs: ScanJob[];
  reviewingJob: ScanJob | null;
  startScan: (type: ScanJobType, imageUri?: string, query?: string) => void;
  retryScan: (jobId: string) => void;
  queueForLater: (jobId: string) => void;
  reviewJob: (jobId: string) => void;
  dismissReview: () => void;
  dismissJob: (jobId: string) => void;
  handleSelectBook: (book: EnrichedBook) => Promise<void>;
}

export const ScanJobContext = createContext<ScanJobContextValue>({
  jobs: [],
  reviewingJob: null,
  startScan: () => {},
  retryScan: () => {},
  queueForLater: () => {},
  reviewJob: () => {},
  dismissReview: () => {},
  dismissJob: () => {},
  handleSelectBook: async () => {},
});

export function ScanJobProvider({ children }: { children: React.ReactNode }) {
  const [jobs, setJobs] = useState<ScanJob[]>([]);
  const [reviewingJobId, setReviewingJobId] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const drainingRef = useRef(false);
  const executeScanRef = useRef<(job: ScanJob) => Promise<void>>(async () => {});
  const { showBanner } = useBanner();
  const { t } = useTranslation('scan');

  // Persist jobs whenever they change (after initial load).
  useEffect(() => {
    if (loaded) {
      saveJobs(jobs);
    }
  }, [jobs, loaded]);

  // Load persisted jobs on mount. Reset interrupted searches to pending.
  useEffect(() => {
    (async () => {
      const persisted = await loadJobs();
      const restored = persisted.map((j) =>
        j.status === 'searching' ? { ...j, status: 'pending' as const } : j
      );
      setJobs(restored);
      setLoaded(true);
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

  const startScan = useCallback(
    async (type: ScanJobType, imageUri?: string, query?: string) => {
      try {
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

  // Keep jobs in a ref so retryScan can read the latest state synchronously.
  const jobsRef = useRef(jobs);
  useEffect(() => {
    jobsRef.current = jobs;
  }, [jobs]);

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

  const dismissReview = useCallback(() => {
    setReviewingJobId(null);
  }, []);

  const dismissJob = useCallback(
    (jobId: string) => {
      setJobs((prev) => prev.filter((j) => j.id !== jobId));
      if (reviewingJobId === jobId) setReviewingJobId(null);

      // Clean up persisted image if it exists.
      if (Platform.OS !== 'web') {
        const job = jobs.find((j) => j.id === jobId);
        if (job?.imageUri) {
          import('expo-file-system').then((fs) => {
            fs.deleteAsync(job.imageUri!, { idempotent: true }).catch(() => {});
          });
        }
      }
    },
    [jobs, reviewingJobId]
  );

  const handleSelectBook = useCallback(
    async (book: EnrichedBook) => {
      Sentry.addBreadcrumb({
        category: 'scan',
        message: `Book selected: ${book.title}`,
        level: 'info',
        data: { title: book.title, author: book.author },
      });

      try {
        await api.post('/wishlist', book);
        if (reviewingJobId) {
          dismissJob(reviewingJobId);
        }
        setReviewingJobId(null);
        showBanner({
          message: t('addedMessage', { title: book.title }),
          type: 'success',
          duration: 4000,
        });
      } catch (err) {
        Sentry.captureException(err, {
          tags: { feature: 'scan', action: 'select_book' },
        });
        showBanner({
          message: t('couldNotSaveMessage'),
          type: 'error',
          duration: 4000,
        });
      }
    },
    [reviewingJobId, dismissJob, showBanner, t]
  );

  // Keep executeScan in a ref so useCallbacks always call the latest version
  // without creating circular deps. Updated after every render so closures
  // always capture the current retryScan / queueForLater callbacks.
  useEffect(() => {
    executeScanRef.current = async function executeScan(job: ScanJob) {
      updateJob(job.id, { status: 'searching' });

      try {
        let results: EnrichedBook[];

        if (job.type === 'text') {
          const response = await api.get<EnrichedBook[]>('/books/search', {
            params: { q: job.query },
          });
          results = response.data ?? [];
        } else {
          const formData = new FormData();
          if (Platform.OS === 'web') {
            formData.append('file', job.imageUri as unknown as Blob, 'scan.jpg');
          } else {
            formData.append('file', {
              uri: job.imageUri,
              name: 'scan.jpg',
              type: 'image/jpeg',
            } as unknown as Blob);
          }
          const response = await api.post<EnrichedBook[]>('/scan', formData, {
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
          results = response.data ?? [];
        }

        if (results.length === 0) {
          updateJob(job.id, { status: 'failed', error: 'no_results' });
          showBanner({
            message: t('noBooksFoundTitle'),
            type: 'info',
            duration: 4000,
          });
          return;
        }

        updateJob(job.id, { status: 'complete', results });
        showBanner({
          message: t('bookFound', { title: results[0].title }),
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
  }, [showBanner, t, retryScan, queueForLater]); // eslint-disable-line react-hooks/exhaustive-deps

  const reviewingJob = useMemo(
    () => (reviewingJobId ? (jobs.find((j) => j.id === reviewingJobId) ?? null) : null),
    [reviewingJobId, jobs]
  );

  const value = useMemo(
    () => ({
      jobs,
      reviewingJob,
      startScan,
      retryScan,
      queueForLater,
      reviewJob,
      dismissReview,
      dismissJob,
      handleSelectBook,
    }),
    [
      jobs,
      reviewingJob,
      startScan,
      retryScan,
      queueForLater,
      reviewJob,
      dismissReview,
      dismissJob,
      handleSelectBook,
    ]
  );

  return <ScanJobContext.Provider value={value}>{children}</ScanJobContext.Provider>;
}
