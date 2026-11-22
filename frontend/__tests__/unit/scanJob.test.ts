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

  it('ages a completed job from completedAt, not createdAt', () => {
    // 11 completed jobs; c10 was captured first but completed last, so it is
    // the newest and the job completed first (c0) is the one dismissed.
    const jobs = Array.from({ length: 11 }, (_, i) =>
      job(`c${i}`, {
        status: 'complete',
        createdAt: NOW - 20 * DAY + i,
        completedAt: i === 10 ? NOW : NOW - 10 * DAY + i,
      })
    );
    expect([...jobsToPurge(jobs, NOW)]).toEqual(['c0']);
  });

  it('falls back to createdAt for a completed job with no completedAt', () => {
    const jobs = [
      ...Array.from({ length: 10 }, (_, i) =>
        job(`n${i}`, { status: 'complete', createdAt: NOW - i, completedAt: NOW - i })
      ),
      job('legacy', { status: 'complete', createdAt: NOW - 5 * DAY }),
    ];
    expect([...jobsToPurge(jobs, NOW)]).toEqual(['legacy']);
  });

  it('ages a queued job from queuedAt, so a retried old failure is kept', () => {
    const retried = job('r', {
      status: 'queued',
      createdAt: NOW - 30 * DAY,
      queuedAt: NOW - 1000,
    });
    expect(isQueuedScanStale(retried, NOW)).toBe(false);
    expect(isQueuedScanStale({ ...retried, queuedAt: NOW - 8 * DAY }, NOW)).toBe(true);
    expect(jobsToPurge([retried], NOW).size).toBe(0);
  });

  it('breaks equal timestamps by list position (the list is newest-first)', () => {
    const failed = Array.from({ length: 21 }, (_, i) => job(`f${i}`, { failedAt: NOW }));
    expect([...jobsToPurge(failed, NOW)]).toEqual(['f20']);
    const done = Array.from({ length: 11 }, (_, i) =>
      job(`c${i}`, { status: 'complete', completedAt: NOW })
    );
    expect([...jobsToPurge(done, NOW)]).toEqual(['c10']);
  });
});
