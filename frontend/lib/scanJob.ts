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
  enhanced_scan_credits: number;
}

export interface ScanJob {
  id: string;
  type: ScanJobType;
  status: ScanJobStatus;
  createdAt: number;
  query?: string;
  imageUri?: string;
  results?: EnrichedBook[];
  /** Re-scan with the stronger (credit-costing) model. */
  enhanced?: boolean;
  error?: string;
  retryCount: number;
}

/** A job still waiting to be uploaded/processed (counts toward the queue cap). */
export function isPendingUpload(job: ScanJob): boolean {
  return job.status === 'queued' || job.status === 'pending';
}
