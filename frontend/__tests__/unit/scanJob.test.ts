import {
  jobsToPurge,
  isQueuedScanStale,
  MAX_FAILED_JOBS,
  MAX_UNREVIEWED_JOBS,
  QUEUED_SCAN_MAX_AGE_MS,
  type ScanJob,
} from '../../lib/scanJob';

const NOW = new Date('2026-11-22T12:00:00Z').getTime();
const DAY = 24 * 60 * 60 * 1000;

function job(id: string, extra: Partial<ScanJob> = {}): ScanJob {
  return {
    id,
    type: 'image',
    status: 'failed',
    createdAt: NOW - 1000,
    imageUri: `file:///docs/scan-queue/${id}.jpg`,
    retryCount: 0,
    ...extra,
  };
}

describe('scan queue bounds (#669, G3)', () => {
  it('keeps the budget constants in one place', () => {
    expect(MAX_FAILED_JOBS).toBe(20);
    expect(MAX_UNREVIEWED_JOBS).toBe(10);
    expect(QUEUED_SCAN_MAX_AGE_MS).toBe(7 * DAY);
  });

  it('flags a queued job past 7 days, and only a queued one', () => {
    expect(
      isQueuedScanStale(job('a', { status: 'queued', createdAt: NOW - 7 * DAY - 1 }), NOW)
    ).toBe(true);
    expect(
      isQueuedScanStale(job('a', { status: 'queued', createdAt: NOW - 7 * DAY + 1 }), NOW)
    ).toBe(false);
    expect(isQueuedScanStale(job('a', { status: 'pending', createdAt: 0 }), NOW)).toBe(false);
  });

  it('keeps 20 failed jobs and removes the oldest beyond that', () => {
    const jobs = Array.from({ length: 21 }, (_, i) => job(`f${i}`, { failedAt: NOW - i * 1000 }));
    expect([...jobsToPurge(jobs, NOW)]).toEqual(['f20']);
    expect(jobsToPurge(jobs.slice(0, 20), NOW).size).toBe(0);
  });

  it('keeps 10 completed jobs and removes the oldest beyond that', () => {
    const jobs = Array.from({ length: 11 }, (_, i) =>
      job(`c${i}`, { status: 'complete', createdAt: NOW - i * 1000 })
    );
    expect([...jobsToPurge(jobs, NOW)]).toEqual(['c10']);
  });

  it('never removes the job under review, taking the next oldest instead', () => {
    const jobs = Array.from({ length: 11 }, (_, i) =>
      job(`c${i}`, { status: 'complete', createdAt: NOW - i * 1000 })
    );
    expect([...jobsToPurge(jobs, NOW, 'c10')]).toEqual([]);
    const twelve = [...jobs, job('c11', { status: 'complete', createdAt: NOW - 11000 })];
    expect([...jobsToPurge(twelve, NOW, 'c10')]).toEqual(['c11']);
  });

  it('removes expired failed and stale queued jobs and leaves pending ones', () => {
    const jobs = [
      job('old-failed', { failedAt: NOW - 8 * DAY }),
      job('old-queued', { status: 'queued', createdAt: NOW - 8 * DAY }),
      job('new-queued', { status: 'queued', createdAt: NOW - DAY }),
      job('old-pending', { status: 'pending', createdAt: NOW - 30 * DAY }),
    ];
    expect([...jobsToPurge(jobs, NOW)].sort()).toEqual(['old-failed', 'old-queued']);
  });
});
