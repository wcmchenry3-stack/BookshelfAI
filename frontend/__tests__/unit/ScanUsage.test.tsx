import React from 'react';
import { act, render, waitFor } from '@testing-library/react-native';

import ScanScreen from '../../app/(tabs)/scan';
import SettingsScreen from '../../app/(tabs)/settings';

const mockGet = jest.fn();
jest.mock('../../lib/api', () => ({ api: { get: (...a: unknown[]) => mockGet(...a) } }));

let mockNotice: unknown = null;
jest.mock('../../hooks/useScanJobs', () => ({
  useScanJobs: () => ({
    jobs: [],
    pendingCount: 0,
    isQueueFull: false,
    startScan: jest.fn(),
    scanNotice: mockNotice,
    dismissScanNotice: jest.fn(),
    usageRefreshKey: 0,
  }),
}));

jest.mock('expo-camera', () => ({
  CameraView: () => null,
  useCameraPermissions: () => [{ granted: true }, jest.fn()],
}));
jest.mock('expo-file-system', () => ({ Paths: {}, Directory: jest.fn(), File: jest.fn() }));
jest.mock('../../components/ThemeToggleButton', () => ({ ThemeToggleButton: () => null }));

let mockConnected = true;
jest.mock('../../hooks/useNetworkStatus', () => ({
  useNetworkStatus: () => ({ isConnected: mockConnected }),
}));

const USAGE = {
  used_today: 2,
  daily_limit: 5,
  used_month: 3,
  monthly_limit: 5,
  period_end: '2026-11-01',
};

beforeEach(() => {
  jest.resetAllMocks();
  mockNotice = null;
  mockConnected = true;
});

describe('scan screen usage counter', () => {
  it('renders monthly usage and scans left today from /scan/usage', async () => {
    mockGet.mockResolvedValue({ data: USAGE });
    const { findByText } = await render(<ScanScreen />);
    expect(await findByText('Scans used this month: 3 of 5')).toBeTruthy();
    expect(await findByText('Scans left today: 2')).toBeTruthy();
    expect(mockGet).toHaveBeenCalledWith('/scan/usage');
  });

  it('left today is capped by what remains this month', async () => {
    mockGet.mockResolvedValue({ data: { ...USAGE, used_today: 0, used_month: 4 } });
    const { findByText } = await render(<ScanScreen />);
    expect(await findByText('Scans left today: 1')).toBeTruthy();
  });

  it('hides the counter when the request fails', async () => {
    mockGet.mockRejectedValue(new Error('Network Error'));
    const { queryByTestId } = await render(<ScanScreen />);
    await waitFor(() => expect(mockGet).toHaveBeenCalled());
    expect(queryByTestId('scan-usage')).toBeNull();
  });

  it('does not call the API or show the counter while offline', async () => {
    mockConnected = false;
    const { queryByTestId } = await render(<ScanScreen />);
    expect(mockGet).not.toHaveBeenCalled();
    expect(queryByTestId('scan-usage')).toBeNull();
  });

  it('renders the monthly limit notice with a localized reset date and no upgrade CTA', async () => {
    mockGet.mockResolvedValue({ data: USAGE });
    mockNotice = { kind: 'monthly_limit', limit: 5, periodEnd: '2026-11-01' };
    const { findByText, queryByText } = await render(<ScanScreen />);
    expect(
      await findByText(
        "You've used your 5 scans for this month. Your scans reset on November 1, 2026."
      )
    ).toBeTruthy();
    expect(queryByText(/upgrade/i)).toBeNull();
  });

  it('renders the daily limit notice with the local reset time', async () => {
    mockGet.mockResolvedValue({ data: USAGE });
    mockNotice = { kind: 'daily_limit', resetsAt: Date.UTC(2026, 9, 23) };
    const { findByText } = await render(<ScanScreen />);
    expect(
      await findByText(/^Daily limit reached\. Scanning resets at midnight UTC \(.+ your time\)\.$/)
    ).toBeTruthy();
  });

  it('renders the still-processing notice', async () => {
    mockGet.mockResolvedValue({ data: USAGE });
    mockNotice = { kind: 'still_processing' };
    const { findByText } = await render(<ScanScreen />);
    expect(await findByText('Still processing. Please try again in a moment.')).toBeTruthy();
  });

  it('renders the paused notice', async () => {
    mockGet.mockResolvedValue({ data: USAGE });
    mockNotice = { kind: 'paused' };
    const { findByText } = await render(<ScanScreen />);
    expect(await findByText('Scanning is paused. Please try again later.')).toBeTruthy();
  });
});

describe('settings usage row', () => {
  it('renders used and limit this month', async () => {
    mockGet.mockResolvedValue({ data: USAGE });
    const { findByText } = await render(<SettingsScreen />);
    expect(await findByText('3 of 5 scans used this month')).toBeTruthy();
  });

  it('is hidden when the request fails', async () => {
    mockGet.mockRejectedValue(new Error('nope'));
    const { queryByTestId } = await render(<SettingsScreen />);
    await act(async () => {});
    expect(queryByTestId('settings-scan-usage')).toBeNull();
  });
});
