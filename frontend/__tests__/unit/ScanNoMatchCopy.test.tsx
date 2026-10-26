import React from 'react';
import { act, render, renderHook, waitFor } from '@testing-library/react-native';

import ScanScreen from '../../app/(tabs)/scan';
import SettingsScreen from '../../app/(tabs)/settings';
import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { BannerProvider } from '../../contexts/BannerContext';
import { useBanner } from '../../hooks/useBanner';
import { useScanJobs } from '../../hooks/useScanJobs';
import { noMatchRemainingToday, remainingToday } from '../../components/ScanStatusPanel';
import { LOCALES } from '../../src/i18n/locales';

// #577: two daily counters (found / no-match). The counters show the numbers
// the server sends (never a hardcoded 5), and a no-match result says whether
// it used a scan.

const mockGet = jest.fn();
const mockPost = jest.fn();
jest.mock('../../lib/api', () => ({
  api: {
    get: (...a: unknown[]) => mockGet(...a),
    post: (...a: unknown[]) => mockPost(...a),
  },
}));

const mockSaveJobs = jest.fn();
const mockLoadJobs = jest.fn();
jest.mock('../../lib/scanJobStorage', () => ({
  loadJobs: () => mockLoadJobs(),
  saveJobs: (...a: unknown[]) => mockSaveJobs(...a),
  clearJobs: jest.fn(),
  sweepOrphanedScanFiles: () => Promise.resolve(0),
  deleteScanImage: jest.fn(),
}));
jest.mock('@react-native-community/netinfo', () => ({
  __esModule: true,
  default: {
    fetch: () => Promise.resolve({ isConnected: true }),
    addEventListener: () => () => {},
  },
}));
let mockUuid = 0;
jest.mock('expo-crypto', () => ({ randomUUID: () => `mockUuid-${++mockUuid}` }));
jest.mock('expo-camera', () => ({
  CameraView: () => null,
  useCameraPermissions: () => [{ granted: true }, jest.fn()],
}));
jest.mock('expo-file-system', () => ({ Paths: {}, Directory: jest.fn(), File: jest.fn() }));
jest.mock('../../components/ThemeToggleButton', () => ({ ThemeToggleButton: () => null }));
jest.mock('../../hooks/useNetworkStatus', () => ({
  useNetworkStatus: () => ({ isConnected: true }),
}));

// GET /scan/usage as the server sends it since #577.
function usageBody(foundUsed: number, foundLimit: number, nmUsed: number, nmLimit: number) {
  return {
    used_today: foundUsed,
    daily_limit: foundLimit,
    found_used_today: foundUsed,
    found_daily_limit: foundLimit,
    no_match_used_today: nmUsed,
    no_match_daily_limit: nmLimit,
    used_month: null,
    monthly_limit: null,
    period_end: null,
    enhanced_used_month: null,
    enhanced_monthly_limit: null,
  };
}

beforeEach(() => {
  jest.resetAllMocks();
  mockUuid = 0;
  mockSaveJobs.mockResolvedValue(undefined);
  mockLoadJobs.mockResolvedValue([]);
});

describe('both counters show the numbers the server sends', () => {
  // Deliberately not 5: the limits are ai_settings values (#577).
  const CASES: [string, ReturnType<typeof usageBody>, string, string][] = [
    ['Free defaults', usageBody(2, 5, 1, 5), 'Scans left today: 3', 'Unrecognised photos: 4 left'],
    ['changed limits', usageBody(1, 9, 3, 7), 'Scans left today: 8', 'Unrecognised photos: 4 left'],
    [
      'Premium, both used up',
      usageBody(12, 12, 6, 6),
      'Scans left today: 0',
      'Unrecognised photos: 0 left',
    ],
    [
      'a ceiling lowered below today’s use never goes negative',
      usageBody(4, 2, 5, 1),
      'Scans left today: 0',
      'Unrecognised photos: 0 left',
    ],
  ];

  it.each(CASES)('scan screen: %s', async (_name, usage, found, noMatch) => {
    mockGet.mockResolvedValue({ data: usage });
    const view = await render(<ScanScreen />);
    expect(await view.findByText(found)).toBeTruthy();
    expect(await view.findByText(noMatch)).toBeTruthy();
  });

  it.each(CASES)('settings: %s', async (_name, usage, found, noMatch) => {
    mockGet.mockResolvedValue({ data: usage });
    const view = await render(<SettingsScreen />);
    expect(await view.findByText(found)).toBeTruthy();
    expect(await view.findByText(noMatch)).toBeTruthy();
  });

  it('the found fields win over the legacy daily fields', () => {
    const usage = { ...usageBody(1, 9, 0, 3), used_today: 0, daily_limit: 99 };
    expect(remainingToday(usage)).toBe(8);
    expect(noMatchRemainingToday(usage)).toBe(3);
  });

  it('an older server (daily fields only) shows the scans line and hides the no-match line', async () => {
    mockGet.mockResolvedValue({ data: { used_today: 2, daily_limit: 6 } });
    const view = await render(<ScanScreen />);
    expect(await view.findByText('Scans left today: 4')).toBeTruthy();
    expect(view.queryByTestId('scan-usage-no-match')).toBeNull();
    expect(view.queryByText(/Unrecognised/)).toBeNull();
  });
});

describe('a no-match result says whether it used a scan', () => {
  function wrapper({ children }: { children: React.ReactNode }) {
    return (
      <BannerProvider>
        <ScanJobProvider>{children}</ScanJobProvider>
      </BannerProvider>
    );
  }

  async function bannerAfterScan(data: Record<string, unknown>) {
    const hook = await renderHook(() => ({ scan: useScanJobs(), banner: useBanner() }), {
      wrapper,
    });
    await waitFor(() => expect(mockSaveJobs).toHaveBeenCalled());
    mockPost.mockResolvedValueOnce({ data });
    await act(async () => {
      await hook.result.current.scan.startScan('image', 'file:///a.jpg');
    });
    return hook.result.current.banner.banner?.message;
  }

  const EMPTY = { books: [], enhanced: false, enhanced_scans_remaining: null };

  it.each([
    ['free', "No books found. This one didn't use a scan."],
    ['counted', "No books found. This used one of today's scans."],
    [null, 'No books found'],
    [undefined, 'No books found'],
  ])('no_match %s', async (noMatch, text) => {
    const message = await bannerAfterScan({ ...EMPTY, no_match: noMatch });
    expect(message).toBe(text);
  });
});

describe('every locale has the found / no-match copy', () => {
  const KEYS: Record<string, string[]> = {
    usageNoMatch: ['count'],
    noBooksFoundFree: [],
    noBooksFoundCounted: [],
  };

  it.each(LOCALES.map((l) => l.code))('%s', (code) => {
    const scan = require(`../../src/i18n/locales/${code}/scan.json`);
    for (const [key, params] of Object.entries(KEYS)) {
      const value = scan[key];
      expect([key, typeof value === 'string' && value.length > 0]).toEqual([key, true]);
      expect(value).not.toContain('NEEDS_TRANSLATION');
      for (const p of params) expect([key, value.includes(`{{${p}}}`)]).toEqual([key, true]);
    }
  });

  it.each(['ar', 'he'])('the inactive RTL locale %s has it too', (code) => {
    const scan = require(`../../src/i18n/locales/${code}/scan.json`);
    for (const key of Object.keys(KEYS)) expect(typeof scan[key]).toBe('string');
  });

  it('_meta describes every new key', () => {
    const meta = require('../../src/i18n/locales/_meta/scan.meta.json');
    for (const key of Object.keys(KEYS)) {
      expect([key, typeof meta[key]?.description]).toEqual([key, 'string']);
    }
  });
});
