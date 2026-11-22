import type { EnrichedBook } from '../components/BookCandidatePicker';

// Maximum scans waiting to upload (queued offline or pending). This bounds
// the upload backlog, not total disk use of `scan-queue/`: a job keeps its
// image until it's dismissed (review closed, books added, or job discarded),
// since both retrying and an enhanced re-scan need it.
export const MAX_QUEUE_SIZE = 50;

// Failed scans stay on the phone this long, then are removed with their image (#605).
export const FAILED_SCAN_RETENTION_MS = 7 * 24 * 60 * 60 * 1000;

// On-device photo budget: at most 80 photos and 50 MB (50 waiting + 20 failed
// + 10 completed but unreviewed). See docs/storage-and-retention.md (G3, G5).
/** Failed jobs kept; when one more fails, the oldest is removed with its photo (#669, G3). */
export const MAX_FAILED_JOBS = 20;
/** Completed jobs not yet reviewed that keep their photo; the oldest beyond this is dismissed (#669, G3). */
export const MAX_UNREVIEWED_JOBS = 10;
/** A refused (`queued`) job older than this is removed with its photo (#669, G3). */
export const QUEUED_SCAN_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;
/** A capture is refused when the phone has less free space than this (#670, G5). */
export const LOW_FREE_SPACE_BYTES = 100 * 1024 * 1024;
/** A capture is refused when `scan-queue/` already holds this much (#670, G5). */
export const MAX_QUEUE_BYTES = 50 * 1024 * 1024;

export type ScanJobType = 'image' | 'text';
type ScanJobStatus = 'pending' | 'searching' | 'complete' | 'failed' | 'queued';

/** Body of POST /scan — every distinct book the AI found in the photo. */
export interface ScanResponse {
  books: EnrichedBook[];
  enhanced: boolean;
  /** Premium enhanced scans left today after this scan (the shared daily
   * allowance left, #582); null when the user is not Premium (enhanced scans
   * are Premium-only, #533). */
  enhanced_scans_remaining: number | null;
  /** True when the answer came from the user's saved scan record for this
   * exact photo: no AI call and nothing charged (#585). The app copy is #588. */
  repeat?: boolean;
  /** A standard scan where the model answered but found no book (#577):
   * 'free' = it did not use a scan; 'counted' = it used one because today's
   * allowance for unrecognised photos is used up; null/absent otherwise. */
  no_match?: 'free' | 'counted' | null;
  /** Opaque reference to the scan record holding this answer (#631); absent
   * when none was saved. Kept with each result (`withScanRecord`). */
  scan_record_id?: string;
}

export interface ScanJob {
  id: string;
  type: ScanJobType;
  status: ScanJobStatus;
  createdAt: number;
  query?: string;
  imageUri?: string;
  results?: EnrichedBook[];
  /** Re-scan with the stronger model (Premium; uses an enhanced scan if it finds books). */
  enhanced?: boolean;
  error?: string;
  retryCount: number;
  /**
   * Retry schedule (#604): attempts at POST /scan this photo has used up on
   * a retryable outcome (503 scan_unavailable). Absent = none yet; the next
   * request sends `attempt` = this + 1.
   */
  attempts?: number;
  /** Epoch ms when the job became failed (#605); absent on older saved jobs, which count from `createdAt`. */
  failedAt?: number;
  /** Epoch ms of the photo's next scheduled attempt (#604); absent when none is scheduled. */
  nextAttemptAt?: number;
}

/** A job still waiting to be uploaded/processed (counts toward the queue cap). */
export function isPendingUpload(job: ScanJob): boolean {
  return job.status === 'queued' || job.status === 'pending';
}

/**
 * True when the job is waiting on the retry schedule (#604): queued (or
 * reset to pending by a restart) with a next attempt time.
 */
export function isAwaitingRetry(job: ScanJob): boolean {
  return isPendingUpload(job) && job.nextAttemptAt != null;
}

/**
 * The "no books found" banner for a standard scan (#577): it also says
 * whether the photo used one of today's scans, as the server reported in
 * `no_match`. Without that field (an older server, a repeat, or nothing
 * decided) just `fallback`. Copy from the `scan` namespace.
 */
export function noMatchMessage(
  noMatch: ScanResponse['no_match'],
  fallback: string,
  t: (key: string) => string
): string {
  if (noMatch === 'free') return t('noBooksFoundFree');
  if (noMatch === 'counted') return t('noBooksFoundCounted');
  return fallback;
}

export type RepeatOutcome = 'none' | 'all_in_library' | 'not_in_library' | 'no_match';

/**
 * How the app explains a photo the user already scanned (#588), from the
 * `repeat` / `in_library` fields the server sends (#585). 'none' = not a
 * repeat, so the scan is shown as usual. Nothing here asks the server again.
 */
export function repeatOutcome(
  response: Pick<ScanResponse, 'repeat' | 'books'> | null | undefined
): RepeatOutcome {
  if (!response?.repeat) return 'none';
  const books = response.books ?? [];
  if (books.length === 0) return 'no_match';
  const allIn = books.every((b) => (b.in_library ?? b.already_in_library) === true);
  return allIn ? 'all_in_library' : 'not_in_library';
}

/** When a failed job failed: `failedAt`, or `createdAt` for jobs saved before it existed. */
export function failedTimeOf(job: ScanJob): number {
  return job.failedAt ?? job.createdAt;
}

/** A failed job older than FAILED_SCAN_RETENTION_MS (#605). */
export function isFailedScanExpired(job: ScanJob, now: number = Date.now()): boolean {
  return job.status === 'failed' && now - failedTimeOf(job) > FAILED_SCAN_RETENTION_MS;
}

/** A refused (`queued`) job older than QUEUED_SCAN_MAX_AGE_MS (#669). */
export function isQueuedScanStale(job: ScanJob, now: number = Date.now()): boolean {
  return job.status === 'queued' && now - job.createdAt > QUEUED_SCAN_MAX_AGE_MS;
}

/**
 * Ids of jobs to remove, with their photos (#669): failed scans past 7 days or
 * beyond the newest MAX_FAILED_JOBS, refused scans past 7 days, and completed
 * scans beyond the newest MAX_UNREVIEWED_JOBS. `keepId` (the review that is
 * open) is never chosen.
 */
export function jobsToPurge(
  jobs: readonly ScanJob[],
  now: number = Date.now(),
  keepId?: string | null
): Set<string> {
  const out = new Set<string>();
  for (const j of jobs) {
    if (j.id !== keepId && (isFailedScanExpired(j, now) || isQueuedScanStale(j, now))) {
      out.add(j.id);
    }
  }
  const newestFirst = (a: number, b: number) => b - a;
  const failed = jobs
    .filter((j) => j.status === 'failed' && !out.has(j.id))
    .sort((a, b) => newestFirst(failedTimeOf(a), failedTimeOf(b)));
  for (const j of failed.slice(MAX_FAILED_JOBS)) out.add(j.id);
  const unreviewed = jobs
    .filter((j) => j.status === 'complete' && j.imageUri && j.id !== keepId)
    .sort((a, b) => newestFirst(a.createdAt, b.createdAt));
  for (const j of unreviewed.slice(MAX_UNREVIEWED_JOBS)) out.add(j.id);
  return out;
}
