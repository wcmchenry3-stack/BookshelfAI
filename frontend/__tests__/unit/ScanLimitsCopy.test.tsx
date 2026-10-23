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

// GET /scan/usage as the server sends it since #582: the monthly fields are
// kept for one release, always null.
const USAGE = {
  used_today: 1,
  daily_limit: 9,
  used_month: null,
  monthly_limit: null,
  period_end: null,
  enhanced_used_month: null,
  enhanced_monthly_limit: null,
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

  // #582: there is no monthly limit. A 402 an older server still sends is a
  // generic failure (the job's own error banner), never a monthly notice.
  it.each([
    ['402 scan_limit_reached', refusal(402, 'scan_limit_reached', { limit: 7, used: 7 })],
    ['402 enhanced_limit_reached', refusal(402, 'enhanced_limit_reached', { limit: 20 })],
  ])('%s from an older server shows no limit notice', async (_name, err) => {
    const { state, view } = await noticeFor(err);
    expect(state).toEqual({ kind: 'generic' });
    expect(view.queryByTestId('scan-notice')).toBeNull();
    expect(view.queryByText(/month/i)).toBeNull();
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
    dailyLimit: ['time'],
    stillProcessing: [],
    scanPaused: [],
    enhancedRequiresPremium: [],
    enhancedThrottled: ['time'],
    enhancedThrottledLater: [],
    usageToday: ['count'],
  };
  // #582: the monthly copy is gone from every locale (and its _meta).
  const RETIRED_SCAN_KEYS = [
    'monthlyLimit',
    'monthlyLimitAny',
    'monthlyResets',
    'enhancedLimitReached',
    'usageMonth',
  ];

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
    for (const key of RETIRED_SCAN_KEYS) expect([key, variants(scan, key)]).toEqual([key, []]);
    const settings = require(`../../src/i18n/locales/${code}/settings.json`);
    expect(typeof settings.scanUsageLabel).toBe('string');
    expect(settings.scanUsageMonth).toBeUndefined();
  });

  it('_meta has no entry for the retired monthly copy', () => {
    const scanMeta = require('../../src/i18n/locales/_meta/scan.meta.json');
    const settingsMeta = require('../../src/i18n/locales/_meta/settings.meta.json');
    for (const key of RETIRED_SCAN_KEYS) expect([key, key in scanMeta]).toEqual([key, false]);
    expect('scanUsageMonth' in settingsMeta).toBe(false);
  });
});

describe('the usage counters show the limits the server sends', () => {
  // #582: only the daily counter; no month counter anywhere.
  const CASES: [string, typeof USAGE, string][] = [
    ['Free defaults', { ...USAGE, used_today: 1, daily_limit: 5 }, 'Scans left today: 4'],
    ['Premium', { ...USAGE, used_today: 3, daily_limit: 30 }, 'Scans left today: 27'],
    [
      'limits changed in ai_settings (no release)',
      { ...USAGE, used_today: 2, daily_limit: 9 },
      'Scans left today: 7',
    ],
    [
      'a limit lowered below today’s usage never shows a negative count',
      { ...USAGE, used_today: 4, daily_limit: 1 },
      'Scans left today: 0',
    ],
  ];

  it.each(CASES)('scan screen: %s', async (_name, usage, today) => {
    mockGet.mockResolvedValue({ data: usage });
    const { findByText, queryByText } = await render(<ScanScreen />);
    expect(await findByText(today)).toBeTruthy();
    expect(queryByText(/month/i)).toBeNull();
  });

  it.each(CASES)('settings: %s', async (_name, usage, today) => {
    mockGet.mockResolvedValue({ data: usage });
    const { findByText, queryByText } = await render(<SettingsScreen />);
    expect(await findByText(today)).toBeTruthy();
    expect(queryByText(/month/i)).toBeNull();
  });

  it('a heavy month of use changes nothing: the counter is the day only', async () => {
    // An older server still sending monthly numbers: they are ignored.
    mockGet.mockResolvedValue({
      data: { ...USAGE, used_today: 2, daily_limit: 5, used_month: 140, monthly_limit: 5 },
    });
    const { findByText, queryByText } = await render(<ScanScreen />);
    expect(await findByText('Scans left today: 3')).toBeTruthy();
    expect(queryByText(/140/)).toBeNull();
  });
});
