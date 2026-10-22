import React from 'react';
import { render, waitFor } from '@testing-library/react-native';

import ScanScreen from '../../app/(tabs)/scan';
import { enhancedScansLeft, type ScanUsage } from '../../hooks/useScanUsage';

// #533: the enhanced allowance left comes from GET /scan/usage as well as from
// scan responses; the scan screen hands it to the scan-jobs context, which
// offers an enhanced re-scan only when it is known to be > 0.

const mockGet = jest.fn();
jest.mock('../../lib/api', () => ({ api: { get: (...a: unknown[]) => mockGet(...a) } }));

const mockSync = jest.fn();
jest.mock('../../hooks/useScanJobs', () => ({
  useScanJobs: () => ({
    jobs: [],
    pendingCount: 0,
    isQueueFull: false,
    startScan: jest.fn(),
    resumeQueue: jest.fn(),
    scanNotice: null,
    dismissScanNotice: jest.fn(),
    usageRefreshKey: 0,
    syncEnhancedScansRemaining: (...a: unknown[]) => mockSync(...a),
  }),
}));
jest.mock('../../hooks/useBanner', () => ({ useBanner: () => ({ showBanner: jest.fn() }) }));
jest.mock('expo-camera', () => ({
  CameraView: () => null,
  useCameraPermissions: () => [{ granted: true }, jest.fn()],
}));
jest.mock('expo-file-system', () => ({ Paths: {}, Directory: jest.fn(), File: jest.fn() }));
jest.mock('../../components/ThemeToggleButton', () => ({ ThemeToggleButton: () => null }));
jest.mock('../../hooks/useNetworkStatus', () => ({
  useNetworkStatus: () => ({ isConnected: true }),
}));

const USAGE: ScanUsage = {
  used_today: 1,
  daily_limit: 30,
  used_month: 4,
  monthly_limit: 300,
  period_end: '2026-11-01',
};

beforeEach(() => {
  jest.resetAllMocks();
});

describe('enhancedScansLeft', () => {
  it('is the enhanced limit minus what was used, never negative', () => {
    expect(
      enhancedScansLeft({ ...USAGE, enhanced_used_month: 3, enhanced_monthly_limit: 20 })
    ).toBe(17);
    expect(
      enhancedScansLeft({ ...USAGE, enhanced_used_month: 25, enhanced_monthly_limit: 20 })
    ).toBe(0);
  });

  it('is 0 for a user who is not Premium (limit 0)', () => {
    expect(enhancedScansLeft({ ...USAGE, enhanced_used_month: 0, enhanced_monthly_limit: 0 })).toBe(
      0
    );
  });

  it('is unknown without usage or without the enhanced fields', () => {
    expect(enhancedScansLeft(null)).toBeNull();
    expect(enhancedScansLeft(USAGE)).toBeNull();
  });
});

describe('scan screen syncs the enhanced scans left', () => {
  it('passes the usage counters to the scan-jobs context', async () => {
    mockGet.mockResolvedValue({
      data: { ...USAGE, enhanced_used_month: 2, enhanced_monthly_limit: 20 },
    });
    await render(<ScanScreen />);
    await waitFor(() => expect(mockSync).toHaveBeenCalledWith(18));
  });

  it('passes 0 for a user who is not Premium', async () => {
    mockGet.mockResolvedValue({
      data: { ...USAGE, enhanced_used_month: 0, enhanced_monthly_limit: 0 },
    });
    await render(<ScanScreen />);
    await waitFor(() => expect(mockSync).toHaveBeenCalledWith(0));
  });

  it('does not sync when the server sent no enhanced fields', async () => {
    mockGet.mockResolvedValue({ data: USAGE });
    await render(<ScanScreen />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledWith('/scan/usage'));
    expect(mockSync).not.toHaveBeenCalled();
  });
});
