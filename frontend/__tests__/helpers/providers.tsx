import React from 'react';
import { act, renderHook } from '@testing-library/react-native';

import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { BannerProvider } from '../../contexts/BannerContext';
import { useBanner } from '../../hooks/useBanner';
import { useScanJobs } from '../../hooks/useScanJobs';
import { scanMocks } from './scanJobMocks';

/** `renderHook` wrapper: the banner and scan-job providers the scan flow needs. */
export function ScanJobsWrapper({ children }: { children: React.ReactNode }) {
  return (
    <BannerProvider>
      <ScanJobProvider>{children}</ScanJobProvider>
    </BannerProvider>
  );
}

/**
 * Render `useScanJobs()`. The caller waits for the mount effect (loadJobs,
 * then the first saveJobs) to settle with
 * `await waitFor(() => expect(saveJobs).toHaveBeenCalled())`, so the
 * assertion stays in the test file.
 */
export function renderScanJobs() {
  return renderHook(() => useScanJobs(), { wrapper: ScanJobsWrapper });
}

/** Like `renderScanJobs`, but returns `{ scan, banner }` so a test can read the banner too. */
export function renderScanJobsWithBanner() {
  return renderHook(() => ({ scan: useScanJobs(), banner: useBanner() }), {
    wrapper: ScanJobsWrapper,
  });
}

/** Fire the NetInfo listener (as if the device reconnected) and let the drain run. */
export async function reconnectAndDrain(ms = 10000) {
  await act(async () => {
    scanMocks.netListener?.({ isConnected: true });
    // Well past the 2 s delay between drained jobs.
    await jest.advanceTimersByTimeAsync(ms);
  });
}
