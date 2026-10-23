import React from 'react';
import { act, renderHook, waitFor } from '@testing-library/react-native';
import i18n from 'i18next';

import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { BannerProvider } from '../../contexts/BannerContext';
import { useScanJobs } from '../../hooks/useScanJobs';
import {
  classifyScanError,
  hasRefusedJobs,
  nextUtcMidnight,
  REFUSAL_ERRORS,
  scanNoticeMessage,
  toScanNotice,
} from '../../lib/scanErrors';
import fs from 'fs';
import path from 'path';

// #586: 429 scan_requests_daily_limit, the per-user daily /scan request limit
// (every request counts, new photo or repeat). A refusal that resets at UTC
// midnight, like daily_limit, but its own state and its own message.

const mockPost = jest.fn();
jest.mock('../../lib/api', () => ({
  api: { post: (...a: unknown[]) => mockPost(...a), get: jest.fn() },
}));
const mockSaveJobs = jest.fn();
const mockLoadJobs = jest.fn();
const mockNetFetch = jest.fn();
let mockNetListener: ((s: { isConnected: boolean }) => void) | null = null;
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
    fetch: () => mockNetFetch(),
    addEventListener: (cb: (s: { isConnected: boolean }) => void) => {
      mockNetListener = cb;
      return () => {};
    },
  },
}));
let mockUuid = 0;
jest.mock('expo-crypto', () => ({ randomUUID: () => `mockUuid-${++mockUuid}` }));

const CODE = 'scan_requests_daily_limit';
// Every locale bundle on disk (13, including those not yet offered in the
// language picker).
const LOCALE_DIRS = fs
  .readdirSync(path.join(__dirname, '../../src/i18n/locales'), { withFileTypes: true })
  .filter((d) => d.isDirectory() && d.name !== '_meta')
  .map((d) => d.name);

function refusal(status: number, detail: string, headers = {}) {
  return { response: { status, data: { detail }, headers } };
}

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <BannerProvider>
      <ScanJobProvider>{children}</ScanJobProvider>
    </BannerProvider>
  );
}

async function setup() {
  const hook = await renderHook(() => useScanJobs(), { wrapper });
  await waitFor(() => expect(mockSaveJobs).toHaveBeenCalled());
  return hook;
}

beforeEach(() => {
  jest.resetAllMocks();
  mockUuid = 0;
  mockSaveJobs.mockResolvedValue(undefined);
  mockLoadJobs.mockResolvedValue([]);
  mockNetFetch.mockResolvedValue({ isConnected: true });
});

afterEach(() => {
  jest.useRealTimers();
});

describe('classifyScanError', () => {
  it('maps 429 scan_requests_daily_limit to its own state with Retry-After', () => {
    expect(classifyScanError(refusal(429, CODE, { 'retry-after': '3600' }))).toEqual({
      kind: 'requests_daily_limit',
      retryAfterSeconds: 3600,
    });
    expect(classifyScanError(refusal(429, CODE))).toEqual({
      kind: 'requests_daily_limit',
      retryAfterSeconds: undefined,
    });
  });

  it('is never the daily scan allowance or a generic failure', () => {
    const state = classifyScanError(refusal(429, CODE, { 'retry-after': '60' }));
    expect(state.kind).not.toBe('daily_limit');
    expect(state.kind).not.toBe('generic');
    expect(classifyScanError(refusal(429, 'daily_scan_limit')).kind).toBe('daily_limit');
  });

  it('only on a 429', () => {
    expect(classifyScanError(refusal(503, CODE))).toEqual({ kind: 'generic' });
  });
});

describe('toScanNotice', () => {
  const now = Date.UTC(2026, 9, 23, 18, 0);

  it('resets at Retry-After, else the next UTC midnight', () => {
    expect(toScanNotice({ kind: 'requests_daily_limit', retryAfterSeconds: 120 }, now)).toEqual({
      kind: 'requests_daily_limit',
      resetsAt: now + 120_000,
    });
    expect(toScanNotice({ kind: 'requests_daily_limit' }, now)).toEqual({
      kind: 'requests_daily_limit',
      resetsAt: nextUtcMidnight(now),
    });
    expect(toScanNotice({ kind: 'requests_daily_limit', retryAfterSeconds: 0 }, now)).toEqual({
      kind: 'requests_daily_limit',
      resetsAt: Date.UTC(2026, 9, 24),
    });
  });
});

describe('copy', () => {
  const t = (key: string, o?: Record<string, unknown>) => `${key}:${JSON.stringify(o ?? {})}`;

  it('has its own line naming the local reset time', () => {
    const resetsAt = Date.UTC(2026, 9, 24);
    const time = new Date(resetsAt).toLocaleTimeString('en', {
      hour: 'numeric',
      minute: '2-digit',
    });
    expect(scanNoticeMessage({ kind: 'requests_daily_limit', resetsAt }, t, 'en')).toBe(
      `requestsDailyLimit:${JSON.stringify({ time })}`
    );
  });

  it.each(LOCALE_DIRS)('is translated in %s, with the time placeholder', (code) => {
    const scan = require(`../../src/i18n/locales/${code}/scan.json`);
    expect(typeof scan.requestsDailyLimit).toBe('string');
    expect(scan.requestsDailyLimit).toContain('{{time}}');
    expect(scan.requestsDailyLimit).not.toContain('NEEDS_TRANSLATION');
    expect(scan.requestsDailyLimit).not.toEqual(scan.dailyLimit);
  });

  it('covers all 13 locale bundles and has a character limit in _meta', () => {
    expect(LOCALE_DIRS).toHaveLength(13);
    const meta = require('../../src/i18n/locales/_meta/scan.meta.json');
    expect(meta.requestsDailyLimit.characterLimit).toBeGreaterThan(0);
  });

  it('reads as English in the en bundle', () => {
    const msg = i18n.t('requestsDailyLimit', { ns: 'scan', lng: 'en', time: '5:00 PM' });
    expect(msg).toBe("You've reached today's scan limit. Try again after 5:00 PM.");
  });
});

describe('queue behaviour', () => {
  it('a job refused for it counts as refused', () => {
    expect(REFUSAL_ERRORS).toContain('requests_daily_limit');
    expect(hasRefusedJobs([{ status: 'queued', error: 'requests_daily_limit' }])).toBe(true);
    expect(hasRefusedJobs([{ status: 'failed', error: 'requests_daily_limit' }])).toBe(false);
  });

  it('sets the notice, re-queues the photo and leaves the usage counters alone', async () => {
    const { result } = await setup();
    const k0 = result.current.usageRefreshKey;
    mockPost.mockRejectedValueOnce(refusal(429, CODE, { 'retry-after': '3600' }));
    await act(async () => {
      await result.current.startScan('image', 'file:///a.jpg');
    });
    expect(result.current.scanNotice?.kind).toBe('requests_daily_limit');
    const n = result.current.scanNotice as { resetsAt: number };
    expect(Math.abs(n.resetsAt - (Date.now() + 3_600_000))).toBeLessThan(5000);
    expect(result.current.jobs[0]).toEqual(
      expect.objectContaining({ status: 'queued', error: 'requests_daily_limit', retryCount: 0 })
    );
    expect(result.current.pendingCount).toBe(1);
    // Refused before anything was taken: nothing to refresh.
    expect(result.current.usageRefreshKey).toBe(k0);
    expect(mockPost).toHaveBeenCalledTimes(1);
  });

  it('stops the drain at the first refusal and leaves the rest queued', async () => {
    const queued = [1, 2, 3].map((i) => ({
      id: `q-${i}`,
      type: 'image' as const,
      status: 'queued' as const,
      createdAt: i,
      imageUri: `file:///q${i}.jpg`,
      retryCount: 0,
    }));
    mockLoadJobs.mockResolvedValue(queued);
    mockPost.mockRejectedValue(refusal(429, CODE, { 'retry-after': '60' }));
    const { result } = await setup();
    jest.useFakeTimers();
    await act(async () => {
      mockNetListener?.({ isConnected: true });
      await jest.advanceTimersByTimeAsync(10000);
    });
    expect(result.current.scanNotice?.kind).toBe('requests_daily_limit');
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(result.current.jobs.map((j) => j.status)).toEqual(['queued', 'queued', 'queued']);
  });
});
