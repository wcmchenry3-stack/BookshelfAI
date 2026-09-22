import type { EnrichedBook } from '../components/BookCandidatePicker';

// Maximum scans waiting to upload (queued offline or pending). This bounds
// the upload backlog, not total disk use of `scan-queue/`: a completed job's
// image is deleted once its results arrive (see executeScan), but a failed
// job keeps its image until it's retried or dismissed, since retrying needs it.
export const MAX_QUEUE_SIZE = 50;

export type ScanJobType = 'image' | 'text';
export type ScanJobStatus = 'pending' | 'searching' | 'complete' | 'failed' | 'queued';

export interface ScanJob {
  id: string;
  type: ScanJobType;
  status: ScanJobStatus;
  createdAt: number;
  query?: string;
  imageUri?: string;
  results?: EnrichedBook[];
  error?: string;
  retryCount: number;
}

/** A job still waiting to be uploaded/processed (counts toward the queue cap). */
export function isPendingUpload(job: ScanJob): boolean {
  return job.status === 'queued' || job.status === 'pending';
}
