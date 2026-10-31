import React from 'react';
import { act, render, waitFor } from '@testing-library/react-native';

import { ScanJobProvider, type ScanJobContextValue } from '../../contexts/ScanJobContext';
import { BannerProvider } from '../../contexts/BannerContext';
import { useScanJobs } from '../../hooks/useScanJobs';

// #805: moving the queue and review logic into hooks must not make the
// provider publish more context values or recreate its callbacks. One full
// scan lifecycle (load, capture, upload, open the review, close it) is
// measured against dev at 40fd4aa: 6 consumer renders, and the reconnect
// listener subscribed twice (mount, then once when the queue has loaded).
const RENDERS_BEFORE_SPLIT = 6;
const NETINFO_SUBSCRIPTIONS_BEFORE_SPLIT = 2;

const mockPost = jest.fn();
jest.mock('../../hooks/useAuth', () => ({ useAuth: () => ({ isAuthenticated: true }) }));
jest.mock('../../lib/api', () => ({
  api: { post: (...a: unknown[]) => mockPost(...a), get: jest.fn() },
}));
const mockSaveJobs = jest.fn();
jest.mock('../../lib/scanJobStorage', () => ({
  loadJobs: () => Promise.resolve([]),
  saveJobs: (...a: unknown[]) => mockSaveJobs(...a),
  clearJobs: jest.fn(),
  sweepOrphanedScanFiles: () => Promise.resolve(0),
  deleteScanImage: jest.fn(),
}));
const mockAddEventListener = jest.fn(() => () => {});
jest.mock('@react-native-community/netinfo', () => ({
  __esModule: true,
  default: {
    fetch: () => Promise.resolve({ isConnected: true }),
    addEventListener: () => mockAddEventListener(),
  },
}));
jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureException: jest.fn(), captureMessage: jest.fn() },
}));
jest.mock('expo-crypto', () => ({ randomUUID: () => 'job-1' }));

it('one scan lifecycle does not re-render consumers more than before the split', async () => {
  mockPost.mockResolvedValue({
    data: {
      books: [{ title: 'Dune', author: 'Herbert' }],
      enhanced: false,
      enhanced_scans_remaining: null,
    },
  });
  let renders = 0;
  let ctx: ScanJobContextValue | null = null;
  let first: ScanJobContextValue | null = null;
  function Consumer() {
    ctx = useScanJobs();
    if (!first) first = ctx;
    renders += 1;
    return null;
  }
  await render(
    <BannerProvider>
      <ScanJobProvider>
        <Consumer />
      </ScanJobProvider>
    </BannerProvider>
  );
  await waitFor(() => expect(mockSaveJobs).toHaveBeenCalled());

  await act(async () => {
    ctx!.startScan('image', 'file:///photo.jpg');
  });
  await waitFor(() => expect(ctx!.jobs[0]?.status).toBe('complete'));
  await act(async () => {
    ctx!.reviewJob('job-1');
  });
  expect(ctx!.reviewingJob?.id).toBe('job-1');
  await act(async () => {
    ctx!.dismissReview();
  });
  expect(ctx!.jobs).toEqual([]);
  expect(renders).toBeLessThanOrEqual(RENDERS_BEFORE_SPLIT);
  // An unstable drainQueue would re-subscribe the listener on every render.
  expect(mockAddEventListener).toHaveBeenCalledTimes(NETINFO_SUBSCRIPTIONS_BEFORE_SPLIT);
  // The queue callbacks keep their identity across the whole lifecycle.
  const last = ctx!;
  expect(last.resumeQueue).toBe(first!.resumeQueue);
  expect(last.startScan).toBe(first!.startScan);
  expect(last.retryScan).toBe(first!.retryScan);
  expect(last.restartScanSchedule).toBe(first!.restartScanSchedule);
});
