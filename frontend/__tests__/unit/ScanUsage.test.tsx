import React from 'react';
import { act, fireEvent, render, waitFor } from '@testing-library/react-native';

import ScanScreen from '../../app/(tabs)/scan';
import SettingsScreen from '../../app/(tabs)/settings';

const mockGet = jest.fn();
jest.mock('../../lib/api', () => ({ api: { get: (...a: unknown[]) => mockGet(...a) } }));

let mockNotice: unknown = null;
const mockResume = jest.fn();
let mockPending = 0;
let mockQueueFull = false;
let mockJobs: unknown[] = [];
const mockShowBanner = jest.fn();
jest.mock('../../hooks/useBanner', () => ({
  useBanner: () => ({ showBanner: (...a: unknown[]) => mockShowBanner(...a) }),
}));
const mockDismiss = jest.fn();
let mockRefreshKey = 0;
jest.mock('../../hooks/useScanJobs', () => ({
  useScanJobs: () => ({
    jobs: mockJobs,
    pendingCount: mockPending,
    isQueueFull: mockQueueFull,
    startScan: jest.fn(),
    resumeQueue: mockResume,
    scanNotice: mockNotice,
    dismissScanNotice: mockDismiss,
    usageRefreshKey: mockRefreshKey,
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
  mockRefreshKey = 0;
  mockPending = 0;
  mockQueueFull = false;
  mockJobs = [];
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

describe('notice actions and refresh', () => {
  it('Try again resumes the queue when scans are waiting', async () => {
    mockGet.mockResolvedValue({ data: USAGE });
    mockNotice = { kind: 'paused' };
    mockPending = 2;
    const { findByText } = await render(<ScanScreen />);
    fireEvent.press(await findByText('Try again'));
    expect(mockResume).toHaveBeenCalledTimes(1);
  });

  it('no Try again when nothing is waiting, Dismiss still shown', async () => {
    mockGet.mockResolvedValue({ data: USAGE });
    mockNotice = { kind: 'paused' };
    const { findByText, queryByText } = await render(<ScanScreen />);
    expect(await findByText('Dismiss')).toBeTruthy();
    expect(queryByText('Try again')).toBeNull();
  });

  it('keeps the last usage when a refetch fails', async () => {
    mockGet.mockResolvedValueOnce({ data: USAGE });
    const view = await render(<SettingsScreen />);
    expect(await view.findByText('3 of 5 scans used this month')).toBeTruthy();
    mockGet.mockRejectedValueOnce(new Error('blip'));
    mockRefreshKey = 1;
    await view.rerender(<SettingsScreen />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
    expect(view.getByText('3 of 5 scans used this month')).toBeTruthy();
  });

  it('settings refetches when the usage refresh key changes', async () => {
    mockGet.mockResolvedValue({ data: USAGE });
    const view = await render(<SettingsScreen />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(1));
    mockRefreshKey = 1;
    await view.rerender(<SettingsScreen />);
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
  });
});

describe('queue-full message and offline Try again', () => {
  async function trySearch(view: Awaited<ReturnType<typeof render>>) {
    await fireEvent.press(view.getByLabelText('Text search mode'));
    await fireEvent.changeText(view.getByLabelText('Book title or author search'), 'Dune');
    await fireEvent.press(view.getByLabelText('Search for book'));
  }

  it('says wait for uploads when the queue is full and nothing is limited', async () => {
    mockGet.mockResolvedValue({ data: USAGE });
    mockQueueFull = true;
    const view = await render(<ScanScreen />);
    await trySearch(view);
    expect(mockShowBanner).toHaveBeenCalledWith(
      expect.objectContaining({
        message: 'Queue full. Wait for pending scans to upload before adding more.',
      })
    );
  });

  it('does not say wait for uploads while the scan limit is the reason', async () => {
    mockGet.mockResolvedValue({ data: USAGE });
    mockQueueFull = true;
    mockNotice = { kind: 'monthly_limit', limit: 5 };
    const view = await render(<ScanScreen />);
    await trySearch(view);
    expect(mockShowBanner).toHaveBeenCalledWith(
      expect.objectContaining({
        message:
          "You've reached your scan limit. Your saved photos will be sent when scanning is available again.",
      })
    );
  });

  it('uses the limit message when queued jobs carry a refusal and the notice is gone', async () => {
    mockGet.mockResolvedValue({ data: USAGE });
    mockQueueFull = true;
    mockJobs = [{ id: 'a', status: 'queued', error: 'daily_limit' }];
    const view = await render(<ScanScreen />);
    await trySearch(view);
    expect(mockShowBanner).toHaveBeenCalledWith(
      expect.objectContaining({ message: expect.stringContaining('scan limit') })
    );
  });

  it('Try again while offline shows the offline message and keeps the notice', async () => {
    mockConnected = false;
    mockNotice = { kind: 'paused' };
    mockPending = 2;
    const { findByText, getByTestId } = await render(<ScanScreen />);
    fireEvent.press(await findByText('Try again'));
    expect(mockResume).not.toHaveBeenCalled();
    expect(mockShowBanner).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'This action requires an internet connection.' })
    );
    expect(getByTestId('scan-notice')).toBeTruthy();
  });
});

describe('Send now button', () => {
  it('shows with pending scans and no notice, and resumes the queue', async () => {
    mockGet.mockResolvedValue({ data: USAGE });
    mockPending = 2;
    const { findByText, queryByTestId } = await render(<ScanScreen />);
    expect(queryByTestId('scan-notice')).toBeNull();
    fireEvent.press(await findByText('Send now'));
    expect(mockResume).toHaveBeenCalledTimes(1);
  });

  it('is hidden offline', async () => {
    mockConnected = false;
    mockPending = 2;
    const { queryByText } = await render(<ScanScreen />);
    expect(queryByText('Send now')).toBeNull();
  });

  it('is hidden with nothing pending', async () => {
    mockGet.mockResolvedValue({ data: USAGE });
    const { queryByText } = await render(<ScanScreen />);
    expect(queryByText('Send now')).toBeNull();
  });
});
