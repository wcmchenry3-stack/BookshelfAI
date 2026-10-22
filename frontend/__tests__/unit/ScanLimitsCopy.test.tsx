import React from 'react';
import { act, render } from '@testing-library/react-native';
import i18n from 'i18next';

import ScanScreen from '../../app/(tabs)/scan';
import SettingsScreen from '../../app/(tabs)/settings';
import {
  classifyScanError,
  enhancedRefusalMessage,
  toScanNotice,
  type ScanUiState,
} from '../../lib/scanErrors';
import { LOCALES } from '../../src/i18n/locales';

// #584: the app side of the scan limits matrix (backend/tests/limits/). Every
// refusal code the client handles today shows its translated message, and the
// usage counters show the limits the server sends, never numbers of their own.

const mockGet = jest.fn();
jest.mock('../../lib/api', () => ({ api: { get: (...a: unknown[]) => mockGet(...a) } }));

let mockNotice: unknown = null;
jest.mock('../../hooks/useScanJobs', () => ({
  useScanJobs: () => ({
    jobs: [],
    pendingCount: 0,
    isQueueFull: false,
    startScan: jest.fn(),
    resumeQueue: jest.fn(),
    scanNotice: mockNotice,
    dismissScanNotice: jest.fn(),
    usageRefreshKey: 0,
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

const USAGE = {
  used_today: 1,
  daily_limit: 9,
  used_month: 2,
  monthly_limit: 41,
  period_end: '2026-11-01',
};

function refusal(status: number, detail: string, extra = {}, headers = {}) {
  return { response: { status, data: { detail, ...extra }, headers } };
}

beforeEach(() => {
  jest.resetAllMocks();
  mockNotice = null;
  mockGet.mockResolvedValue({ data: USAGE });
});

/** What the scan screen shows for a refused POST /scan, end to end:
 * classify the error, make the notice (retries exhausted), render it. */
async function noticeFor(err: unknown) {
  const state = classifyScanError(err);
  mockNotice = toScanNotice(state, Date.UTC(2026, 9, 22, 12), true);
  const view = await render(<ScanScreen />);
  return { state, view };
}

describe('every scan refusal shows its translated message', () => {
  const NOTICES: [string, unknown, string | RegExp][] = [
    [
      '402 scan_limit_reached',
      refusal(402, 'scan_limit_reached', { limit: 7, used: 7, period_end: '2026-11-01' }),
      "You've used your 7 scans for this month. Your scans reset on November 1, 2026.",
    ],
    [
      '402 scan_limit_reached, limit 1',
      refusal(402, 'scan_limit_reached', { limit: 1, used: 1, period_end: '2027-01-01' }),
      "You've used your 1 scan for this month. Your scans reset on January 1, 2027.",
    ],
    [
      '402 scan_limit_reached without numbers',
      refusal(402, 'scan_limit_reached'),
      "You've used all your scans for this month.",
    ],
    [
      '429 daily_scan_limit',
      refusal(429, 'daily_scan_limit', {}, { 'retry-after': '43200' }),
      /^Daily limit reached\. Scanning resets at midnight UTC \(.+ your time\)\.$/,
    ],
    [
      '429 scan_in_progress (retries used up)',
      refusal(429, 'scan_in_progress', {}, { 'retry-after': '2' }),
      'Still processing. Please try again in a moment.',
    ],
    // The 503 codes backend/app/api/scan.py sends today, listed here rather
    // than taken from PAUSED_CODES so a code dropped from the client fails.
    ...[
      'scan_disabled',
      'enhanced_scan_disabled',
      'scan_capacity',
      'ai_budget_reached',
      'ai_unavailable',
      'scan_unavailable',
    ].map((code): [string, unknown, string] => [
      `503 ${code}`,
      refusal(503, code),
      'Scanning is paused. Please try again later.',
    ]),
  ];

  it.each(NOTICES)('%s', async (_name, err, text) => {
    const { view } = await noticeFor(err);
    expect(await view.findByText(text)).toBeTruthy();
  });

  // Enhanced refusals are a banner, worded by enhancedRefusalMessage (the
  // ScanJobContext banner path is covered in ScanQuotaFlow.test.tsx).
  const ENHANCED: [string, unknown, string][] = [
    [
      '403 enhanced_requires_premium',
      refusal(403, 'enhanced_requires_premium'),
      'Enhanced scans are part of Premium.',
    ],
    [
      '402 enhanced_limit_reached',
      refusal(402, 'enhanced_limit_reached', { limit: 20, used: 20, period_end: '2026-11-01' }),
      "You've used all your enhanced scans for this month.",
    ],
    [
      '429 enhanced_throttled without Retry-After',
      refusal(429, 'enhanced_throttled'),
      'Several recent enhanced scans found no books. Please try again later.',
    ],
  ];

  it.each(ENHANCED)('%s', (_name, err, text) => {
    const state = classifyScanError(err) as Extract<ScanUiState, { kind: 'enhanced_refused' }>;
    expect(state.kind).toBe('enhanced_refused');
    expect(toScanNotice(state)).toBeNull();
    expect(enhancedRefusalMessage(state, i18n.getFixedT('en', 'scan'), 'en')).toBe(text);
  });

  it('429 enhanced_throttled names the local time from Retry-After', () => {
    const now = Date.UTC(2026, 9, 22, 12);
    const state = classifyScanError(
      refusal(429, 'enhanced_throttled', {}, { 'retry-after': '5400' })
    ) as Extract<ScanUiState, { kind: 'enhanced_refused' }>;
    const time = new Date(now + 5_400_000).toLocaleTimeString('en', {
      hour: 'numeric',
      minute: '2-digit',
    });
    expect(enhancedRefusalMessage(state, i18n.getFixedT('en', 'scan'), 'en', now)).toBe(
      `Several recent enhanced scans found no books. Try again after ${time}.`
    );
  });

  it('a refusal renders in the user’s language, not English', async () => {
    const de = require('../../src/i18n/locales/de/scan.json');
    const en = require('../../src/i18n/locales/en/scan.json');
    expect(de.scanPaused).not.toBe(en.scanPaused);
    // Non-en locales load lazily in the app; here the bundle is added as the
    // backend would add it.
    i18n.addResourceBundle('de', 'scan', de, true, true);
    await act(async () => {
      await i18n.changeLanguage('de');
    });
    try {
      const { view } = await noticeFor(refusal(503, 'ai_budget_reached'));
      expect(await view.findByText(de.scanPaused)).toBeTruthy();
    } finally {
      await act(async () => {
        await i18n.changeLanguage('en');
      });
      i18n.removeResourceBundle('de', 'scan');
    }
  });
});

describe('every active locale has the refusal and usage copy', () => {
  // key -> placeholders the translation must keep (the server's numbers and
  // dates reach the user only through these).
  const SCAN_KEYS: Record<string, string[]> = {
    monthlyLimit: ['count'],
    monthlyLimitAny: [],
    monthlyResets: ['date'],
    dailyLimit: ['time'],
    stillProcessing: [],
    scanPaused: [],
    enhancedRequiresPremium: [],
    enhancedLimitReached: [],
    enhancedThrottled: ['time'],
    enhancedThrottledLater: [],
    usageMonth: ['used', 'limit'],
    usageToday: ['count'],
  };

  function variants(data: Record<string, string>, key: string): [string, string][] {
    return Object.entries(data).filter(([k]) => k === key || k.startsWith(`${key}_`));
  }

  it.each(LOCALES.map((l) => l.code))('%s', (code) => {
    const scan = require(`../../src/i18n/locales/${code}/scan.json`);
    for (const [key, params] of Object.entries(SCAN_KEYS)) {
      const found = variants(scan, key);
      expect([key, found.length > 0]).toEqual([key, true]);
      for (const [k, value] of found) {
        expect([k, typeof value === 'string' && value.length > 0]).toEqual([k, true]);
        expect(value).not.toContain('NEEDS_TRANSLATION');
        for (const p of params) expect([k, value.includes(`{{${p}}}`)]).toEqual([k, true]);
      }
    }
    const settings = require(`../../src/i18n/locales/${code}/settings.json`);
    expect(settings.scanUsageMonth).toContain('{{used}}');
    expect(settings.scanUsageMonth).toContain('{{limit}}');
  });
});

describe('the usage counters show the limits the server sends', () => {
  const CASES: [string, typeof USAGE, string, string, string][] = [
    [
      'Free defaults',
      { ...USAGE, used_today: 1, daily_limit: 5, used_month: 1, monthly_limit: 5 },
      'Scans used this month: 1 of 5',
      'Scans left today: 4',
      '1 of 5 scans used this month',
    ],
    [
      'Premium',
      { ...USAGE, used_today: 3, daily_limit: 30, used_month: 120, monthly_limit: 300 },
      'Scans used this month: 120 of 300',
      'Scans left today: 27',
      '120 of 300 scans used this month',
    ],
    [
      'limits changed in ai_settings (no release)',
      { ...USAGE, used_today: 2, daily_limit: 9, used_month: 17, monthly_limit: 41 },
      'Scans used this month: 17 of 41',
      'Scans left today: 7',
      '17 of 41 scans used this month',
    ],
    [
      'a limit lowered below today’s usage never shows a negative count',
      { ...USAGE, used_today: 4, daily_limit: 1, used_month: 4, monthly_limit: 2 },
      'Scans used this month: 4 of 2',
      'Scans left today: 0',
      '4 of 2 scans used this month',
    ],
    [
      'the monthly allowance is the tighter one',
      { ...USAGE, used_today: 0, daily_limit: 30, used_month: 298, monthly_limit: 300 },
      'Scans used this month: 298 of 300',
      'Scans left today: 2',
      '298 of 300 scans used this month',
    ],
  ];

  it.each(CASES)('scan screen: %s', async (_name, usage, month, today) => {
    mockGet.mockResolvedValue({ data: usage });
    const { findByText, getByText } = await render(<ScanScreen />);
    expect(await findByText(month)).toBeTruthy();
    expect(getByText(today)).toBeTruthy();
  });

  it.each(CASES)('settings: %s', async (_name, usage, _month, _today, settingsRow) => {
    mockGet.mockResolvedValue({ data: usage });
    const { findByText } = await render(<SettingsScreen />);
    expect(await findByText(settingsRow)).toBeTruthy();
  });
});
