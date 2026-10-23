import type { EnrichedBook } from '../components/BookCandidatePicker';

// Maximum scans waiting to upload (queued offline or pending). This bounds
// the upload backlog, not total disk use of `scan-queue/`: a job keeps its
// image until it's dismissed (review closed, books added, or job discarded),
// since both retrying and an enhanced re-scan need it.
export const MAX_QUEUE_SIZE = 50;

export type ScanJobType = 'image' | 'text';
export type ScanJobStatus = 'pending' | 'searching' | 'complete' | 'failed' | 'queued';

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
}

/** A job still waiting to be uploaded/processed (counts toward the queue cap). */
export function isPendingUpload(job: ScanJob): boolean {
  return job.status === 'queued' || job.status === 'pending';
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
