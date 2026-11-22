import {
  useCallback,
  useEffect,
  useRef,
  type Dispatch,
  type MutableRefObject,
  type SetStateAction,
} from 'react';
import NetInfo from '@react-native-community/netinfo';

import type { EnrichedBook } from '../components/BookCandidatePicker';
import type { PendingUnverifiedDraft } from '../contexts/ScanJobContext';
import { api } from '../lib/api';
import { apiErrorTags, isReportedCentrally, sanitizeApiError } from '../lib/apiErrorReporting';
import { catalogRefusal, isAlreadyOwnedError } from '../lib/ownership';
import { Sentry } from '../lib/sentry';
import type { ScanExecDeps } from '../lib/scan/types';
import { draftFromBook } from '../lib/unverified';

/** Identifies a scan result within its job (title and author, as read). */
export function candidateKey(book: EnrichedBook): string {
  return `${book.title}\u0000${book.author}`;
}

/**
 * What the review flow needs from ScanJobProvider. The drafts state and the
 * refs below are the provider's own because its sign-out effect resets them.
 */
export type ScanReviewDeps = Pick<
  ScanExecDeps,
  'jobsRef' | 'updateJob' | 'generationRef' | 'abortersRef' | 'showBanner' | 't'
> & {
  reviewingJobId: string | null;
  setReviewingJobId: Dispatch<SetStateAction<string | null>>;
  reviewingJobIdRef: MutableRefObject<string | null>;
  dismissJob: (jobId: string) => void;
  unverifiedDrafts: PendingUnverifiedDraft[];
  setUnverifiedDrafts: Dispatch<SetStateAction<PendingUnverifiedDraft[]>>;
  /** The job whose review to reopen after the "Add it yourself" forms close. */
  reopenReviewJobIdRef: MutableRefObject<string | null>;
  /**
   * Where each draft's book came from, by draft id. Kept after the forms
   * close, so a save still in flight can take its own book out of its scan.
   */
  draftSourcesRef: MutableRefObject<Map<string, { jobId: string | null; key: string }>>;
};

/**
 * The review checklist's actions: add the ticked books to the wishlist
 * (handleAddBooks) and the "Add it yourself" drafts (#616) for books the
 * catalog could not find.
 */
export function useScanReview({
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
}: ScanReviewDeps) {
  const nextDraftIdRef = useRef(0);

  // Keep the review id readable from long-lived closures (the banner action).
  useEffect(() => {
    reviewingJobIdRef.current = reviewingJobId;
  }, [reviewingJobId, reviewingJobIdRef]);

  // "Add it yourself" (#616). One sheet at a time: close the review picker
  // WITHOUT dismissing its job (dismissReview would delete the whole job and
  // photo). A book leaves the job only once its form is saved, so cancelling
  // the form brings the review back with that book still in it.
  const addYourself = useCallback(
    (books: EnrichedBook[]) => {
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
    },
    [
      reviewingJobIdRef,
      jobsRef,
      setReviewingJobId,
      reopenReviewJobIdRef,
      setUnverifiedDrafts,
      draftSourcesRef,
    ]
  );

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
  }, [unverifiedDrafts, dismissJob, reopenReviewJobIdRef, jobsRef, setReviewingJobId]);

  const dismissUnverified = useCallback(() => setUnverifiedDrafts([]), [setUnverifiedDrafts]);

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
    [showBanner, t, updateJob, draftSourcesRef, jobsRef, setUnverifiedDrafts]
  );

  const handleAddBooks = useCallback(
    async (books: EnrichedBook[]) => {
      if (books.length === 0) return;
      const jobId = reviewingJobId;
      const generation = generationRef.current;
      const signedOutSince = () => generationRef.current !== generation;
      const controller = new AbortController();
      abortersRef.current.add(controller);

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
      if (signedOutSince()) {
        abortersRef.current.delete(controller);
        return;
      }
      if (netState.isConnected === false) {
        abortersRef.current.delete(controller);
        showBanner({
          message: t('requiresConnection', { ns: 'common' }),
          type: 'error',
          duration: 4000,
        });
        return;
      }

      const outcomes = await Promise.allSettled(
        books.map((book) => api.post('/wishlist', book, { signal: controller.signal }))
      );
      abortersRef.current.delete(controller);
      // Signed out meanwhile: no banner, review change or Sentry event.
      if (signedOutSince()) return;
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
    [
      reviewingJobId,
      dismissJob,
      updateJob,
      showBanner,
      t,
      addYourself,
      generationRef,
      abortersRef,
      setReviewingJobId,
    ]
  );

  return { handleAddBooks, addYourself, unverifiedSaved, dismissUnverified };
}
