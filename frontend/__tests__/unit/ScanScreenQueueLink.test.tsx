import React from 'react';
import { fireEvent, render } from '@testing-library/react-native';

import ScanScreen from '../../app/(tabs)/scan';

// #605: the pending-uploads badge on the scan screen opens the scan queue.

const mockPush = jest.fn();
jest.mock('expo-router', () => ({
  router: { push: (...a: unknown[]) => mockPush(...a) },
}));
let mockJobs: { status: string }[] = [];
let mockPendingCount = 2;
jest.mock('../../hooks/useScanJobs', () => ({
  useScanJobs: () => ({
    jobs: mockJobs,
    pendingCount: mockPendingCount,
    isQueueFull: false,
    startScan: jest.fn(),
    resumeQueue: jest.fn(),
    scanNotice: null,
    dismissScanNotice: jest.fn(),
    usageRefreshKey: 0,
  }),
}));
jest.mock('../../hooks/useScanUsage', () => ({ useScanUsage: () => null }));
jest.mock('../../hooks/useNetworkStatus', () => ({
  useNetworkStatus: () => ({ isConnected: false }),
}));
jest.mock('../../hooks/useBanner', () => ({ useBanner: () => ({ showBanner: jest.fn() }) }));
jest.mock('expo-camera', () => ({
  CameraView: () => null,
  useCameraPermissions: () => [{ granted: true }, jest.fn()],
}));
jest.mock('expo-file-system', () => ({ Directory: jest.fn(), File: jest.fn(), Paths: {} }));
jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureException: jest.fn(), captureMessage: jest.fn() },
}));
jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      isDark: false,
      colors: {
        background: '#fff',
        surface: '#f5f5f5',
        border: '#ccc',
        text: '#000',
        textSecondary: '#888',
        primary: '#007AFF',
        iconActive: '#000',
      },
      typography: { fontSizeBase: 16 },
      spacing: { md: 16 },
    },
  }),
}));

it('the pending-uploads badge opens the scan queue (even offline)', async () => {
  const { getByLabelText } = await render(<ScanScreen />);
  fireEvent.press(getByLabelText('View queue'));
  expect(mockPush).toHaveBeenCalledWith('/scan-queue');
});

describe('failed scans only', () => {
  afterEach(() => {
    mockJobs = [];
    mockPendingCount = 2;
  });

  it('shows a View queue link with the failed count when nothing is waiting', async () => {
    mockPendingCount = 0;
    mockJobs = [{ status: 'failed' }, { status: 'failed' }, { status: 'complete' }];
    mockPush.mockClear();
    const { getByLabelText } = await render(<ScanScreen />);
    fireEvent.press(getByLabelText('View queue (2 failed)'));
    expect(mockPush).toHaveBeenCalledWith('/scan-queue');
  });

  it('shows no queue link when nothing is waiting or failed', async () => {
    mockPendingCount = 0;
    mockJobs = [{ status: 'complete' }];
    const { queryByLabelText } = await render(<ScanScreen />);
    expect(queryByLabelText(/^View queue/)).toBeNull();
  });
});
