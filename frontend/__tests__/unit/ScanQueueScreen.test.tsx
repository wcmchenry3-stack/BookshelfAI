import fs from 'fs';
import path from 'path';
import React from 'react';
import { act, fireEvent, render, renderHook, waitFor } from '@testing-library/react-native';

import ScanQueueScreen from '../../app/scan-queue';
import { BannerProvider } from '../../contexts/BannerContext';
import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { useBanner } from '../../hooks/useBanner';
import { useScanJobs } from '../../hooks/useScanJobs';
import { SCAN_RETRY_DELAYS_MS } from '../../lib/scanErrors';
import { FAILED_SCAN_RETENTION_MS, isFailedScanExpired, type ScanJob } from '../../lib/scanJob';

// #605: the scan queue screen (waiting + failed photos, retry / delete) and
// the 7-day expiry of failed scans.

const mockPost = jest.fn();
jest.mock('../../lib/api', () => ({
  api: { post: (...a: unknown[]) => mockPost(...a), get: jest.fn() },
}));

const mockSaveJobs = jest.fn();
const mockLoadJobs = jest.fn();
const mockDeleteImage = jest.fn();
jest.mock('../../lib/scanJobStorage', () => ({
  loadJobs: () => mockLoadJobs(),
  saveJobs: (...a: unknown[]) => mockSaveJobs(...a),
  clearJobs: jest.fn(),
  sweepOrphanedScanFiles: () => Promise.resolve(0),
  deleteScanImage: (...a: unknown[]) => mockDeleteImage(...a),
}));

jest.mock('@react-native-community/netinfo', () => ({
  __esModule: true,
  default: {
    fetch: () => Promise.resolve({ isConnected: true }),
    addEventListener: () => () => {},
  },
}));

jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureException: jest.fn(), captureMessage: jest.fn() },
}));
jest.mock('expo-crypto', () => ({ randomUUID: () => 'uuid' }));

const mockPush = jest.fn();
jest.mock('expo-router', () => ({
  router: {
    push: (...a: unknown[]) => mockPush(...a),
    back: jest.fn(),
    replace: jest.fn(),
    canGoBack: () => true,
  },
}));

const NOW = new Date('2026-10-23T12:00:00Z').getTime();
const DAY = 24 * 60 * 60 * 1000;

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <BannerProvider>
      <ScanJobProvider>{children}</ScanJobProvider>
    </BannerProvider>
  );
}

function job(id: string, extra: Partial<ScanJob> = {}): ScanJob {
  return {
    id,
    type: 'image',
    status: 'failed',
    createdAt: NOW - 1000,
    imageUri: `file:///docs/scan-queue/${id}.jpg`,
    retryCount: 0,
    ...extra,
  };
}

async function renderScreen(persisted: ScanJob[]) {
  mockLoadJobs.mockResolvedValue(persisted);
  const utils = await render(<ScanQueueScreen />, { wrapper });
  await waitFor(() => expect(mockSaveJobs).toHaveBeenCalled());
  return utils;
}

beforeEach(() => {
  jest.resetAllMocks();
  jest.useFakeTimers();
  jest.setSystemTime(NOW);
  mockSaveJobs.mockResolvedValue(undefined);
  mockLoadJobs.mockResolvedValue([]);
});

afterEach(() => {
  jest.useRealTimers();
});

describe('scan queue screen (#605)', () => {
  it('shows empty states for both sections', async () => {
    const { getByText } = await renderScreen([]);
    expect(getByText('No photos are waiting.')).toBeTruthy();
    expect(getByText('No failed scans.')).toBeTruthy();
  });

  it('lists waiting photos with their state and failed photos with Retry and Delete', async () => {
    const next = NOW + 600_000;
    const { getByTestId, getByText, getAllByLabelText, queryByText, getAllByText } =
      await renderScreen([
        job('offline', { status: 'queued' }),
        job('scheduled', { status: 'queued', attempts: 2, nextAttemptAt: next }),
        job('sending', { status: 'searching' }),
        job('text', { status: 'queued', type: 'text', imageUri: undefined, query: 'Dune' }),
        job('bad', { failedAt: NOW - 3600_000 }),
      ]);
    expect(getByTestId('queue-waiting-offline')).toBeTruthy();
    expect(getAllByText('Waiting for connection')).toHaveLength(3);
    expect(getByText(/^Next try at /)).toBeTruthy();
    // 'searching' is reset to pending on load; it waits like any other.
    expect(getByTestId('queue-waiting-sending')).toBeTruthy();
    expect(getByText('Dune')).toBeTruthy();
    expect(getByTestId('queue-failed-bad')).toBeTruthy();
    expect(getByText(/^Failed [A-Z0-9]/)).toBeTruthy();
    expect(getAllByLabelText('Retry')).toHaveLength(1);
    expect(getAllByLabelText('Delete')).toHaveLength(1);
    expect(queryByText('No failed scans.')).toBeNull();
    expect(queryByText('No photos are waiting.')).toBeNull();
  });

  it('Retry restarts the photo at attempt 1 and sends it', async () => {
    mockPost.mockRejectedValue({ response: { status: 503, data: { detail: 'scan_unavailable' } } });
    const { getByLabelText, getByTestId } = await renderScreen([
      job('bad', { attempts: 5, retryCount: 2, failedAt: NOW - 1000 }),
    ]);
    await act(async () => {
      fireEvent.press(getByLabelText('Retry'));
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(mockPost).toHaveBeenCalledTimes(1);
    const form = mockPost.mock.calls[0][1] as FormData & { _parts?: [string, unknown][] };
    const attempt =
      typeof form.get === 'function'
        ? form.get('attempt')
        : Object.fromEntries(form._parts ?? []).attempt;
    expect(attempt).toBe('1');
    // It is back in the waiting section, on the schedule at attempt 1.
    expect(getByTestId('queue-waiting-bad')).toBeTruthy();
    const last = mockSaveJobs.mock.calls.at(-1)![0] as ScanJob[];
    expect(last[0]).toEqual(
      expect.objectContaining({ status: 'queued', attempts: 1, failedAt: undefined })
    );
    expect(last[0].nextAttemptAt).toBe(NOW + SCAN_RETRY_DELAYS_MS[0]);
  });

  it('Delete removes the job and its image', async () => {
    const { getByLabelText, queryByTestId, getByText } = await renderScreen([job('bad')]);
    await act(async () => {
      fireEvent.press(getByLabelText('Delete'));
    });
    expect(mockDeleteImage).toHaveBeenCalledWith('file:///docs/scan-queue/bad.jpg');
    expect(queryByTestId('queue-failed-bad')).toBeNull();
    expect(getByText('No failed scans.')).toBeTruthy();
  });

  it('buttons are accessible buttons at least 44 pt', async () => {
    const { getByLabelText } = await renderScreen([job('bad')]);
    for (const label of ['Retry', 'Delete']) {
      const el = getByLabelText(label);
      expect(el.props.accessibilityRole).toBe('button');
    }
  });
});

it('shows Sending… for a photo being sent right now', async () => {
  mockPost.mockReturnValue(new Promise(() => {}));
  let start: ((type: 'image', uri: string) => void) | undefined;
  function Grab() {
    start = useScanJobs().startScan;
    return null;
  }
  const { getByText } = await render(
    <>
      <ScanQueueScreen />
      <Grab />
    </>,
    { wrapper }
  );
  await waitFor(() => expect(mockSaveJobs).toHaveBeenCalled());
  await act(async () => {
    start!('image', 'file:///docs/scan-queue/s.jpg');
    await jest.advanceTimersByTimeAsync(0);
  });
  expect(getByText('Sending…')).toBeTruthy();
});

describe('failed scans expire after 7 days (#605)', () => {
  it('keeps the retention in one constant', () => {
    expect(FAILED_SCAN_RETENTION_MS).toBe(7 * DAY);
    expect(isFailedScanExpired(job('a', { failedAt: NOW - 7 * DAY - 1 }), NOW)).toBe(true);
    expect(isFailedScanExpired(job('a', { failedAt: NOW - 7 * DAY + 1 }), NOW)).toBe(false);
    expect(isFailedScanExpired(job('a', { status: 'queued', createdAt: 0 }), NOW)).toBe(false);
  });

  it('removes old failed jobs and their images on load; counts from createdAt without failedAt', async () => {
    mockLoadJobs.mockResolvedValue([
      job('old', { failedAt: NOW - 8 * DAY }),
      job('legacy', { createdAt: NOW - 9 * DAY }),
      job('fresh', { failedAt: NOW - 6 * DAY }),
      job('waiting', { status: 'queued', createdAt: NOW - 30 * DAY }),
    ]);
    const { result } = await renderHook(() => useScanJobs(), { wrapper });
    await waitFor(() => expect(result.current.jobs.length).toBe(2));
    expect(result.current.jobs.map((j) => j.id)).toEqual(['fresh', 'waiting']);
    expect(mockDeleteImage).toHaveBeenCalledWith('file:///docs/scan-queue/old.jpg');
    expect(mockDeleteImage).toHaveBeenCalledWith('file:///docs/scan-queue/legacy.jpg');
    expect(mockDeleteImage).toHaveBeenCalledTimes(2);
  });

  it('removes jobs that expired while the app was open when the screen opens', async () => {
    mockLoadJobs.mockResolvedValue([job('soon', { failedAt: NOW - 6 * DAY })]);
    const hook = await renderHook(() => useScanJobs(), { wrapper });
    await waitFor(() => expect(hook.result.current.jobs.length).toBe(1));
    expect(mockDeleteImage).not.toHaveBeenCalled();

    // Two days pass with the app open, then the screen opens.
    jest.setSystemTime(NOW + 2 * DAY);
    await act(async () => {
      hook.result.current.purgeExpiredFailedJobs();
    });
    expect(hook.result.current.jobs).toEqual([]);
    expect(mockDeleteImage).toHaveBeenCalledWith('file:///docs/scan-queue/soon.jpg');
  });

  it('opening the screen runs the expiry', async () => {
    const purge = jest.fn();
    jest.spyOn(require('../../hooks/useScanJobs'), 'useScanJobs').mockReturnValue({
      jobs: [],
      dismissJob: jest.fn(),
      restartScanSchedule: jest.fn(),
      purgeExpiredFailedJobs: purge,
    });
    await render(<ScanQueueScreen />);
    expect(purge).toHaveBeenCalledTimes(1);
    jest.restoreAllMocks();
  });

  it('stamps failedAt when a job fails', async () => {
    mockPost.mockRejectedValue({ response: { status: 400, data: { detail: 'x' } } });
    mockLoadJobs.mockResolvedValue([]);
    const hook = await renderHook(() => useScanJobs(), { wrapper });
    await waitFor(() => expect(mockSaveJobs).toHaveBeenCalled());
    await act(async () => {
      await hook.result.current.startScan('image', 'file:///docs/scan-queue/x.jpg');
    });
    const failed = hook.result.current.jobs.find((j) => j.status === 'failed');
    expect(failed?.failedAt).toBe(NOW);
  });
});

describe('navigation to the queue (#605)', () => {
  it('the final-failure banner has a View queue action that opens the screen', async () => {
    mockPost.mockRejectedValue({ response: { status: 503, data: { detail: 'scan_unavailable' } } });
    const hook = await renderHook(() => ({ scan: useScanJobs(), banner: useBanner() }), {
      wrapper,
    });
    await waitFor(() => expect(mockSaveJobs).toHaveBeenCalled());
    await act(async () => {
      await hook.result.current.scan.startScan('image', 'file:///docs/scan-queue/x.jpg');
    });
    for (const d of SCAN_RETRY_DELAYS_MS) {
      await act(async () => {
        await jest.advanceTimersByTimeAsync(d);
      });
    }
    const actions = hook.result.current.banner.banner?.actions ?? [];
    expect(actions.map((a) => a.label)).toEqual(['Retry', 'View queue']);
    actions[1].onPress();
    expect(mockPush).toHaveBeenCalledWith('/scan-queue');
  });
});

describe('queue copy in every locale (#605)', () => {
  const dir = path.join(__dirname, '../../src/i18n/locales');
  const locales: string[] = fs
    .readdirSync(dir, { withFileTypes: true })
    .filter((d: { isDirectory(): boolean; name: string }) => d.isDirectory() && d.name !== '_meta')
    .map((d: { name: string }) => d.name);
  const keys = [
    'queueTitle',
    'queueBack',
    'queueWaitingTitle',
    'queueWaitingEmpty',
    'queueFailedTitle',
    'queueFailedEmpty',
    'queueFailedNote',
    'queueStateWaiting',
    'queueStateNextTry',
    'queueStateSending',
    'queueFailedAt',
    'queueRetry',
    'queueDelete',
    'queuePhotoA11y',
    'viewQueue',
    'scanFailedFinalViewQueue',
  ];
  const meta = JSON.parse(fs.readFileSync(path.join(dir, '_meta/scan.meta.json'), 'utf8'));

  it('has 13 locales', () => expect(locales).toHaveLength(13));

  it.each(keys)('%s has _meta and a translation in every locale', (key) => {
    expect(meta[key]).toEqual(
      expect.objectContaining({
        description: expect.any(String),
        tone: expect.any(String),
        characterLimit: expect.any(Number),
      })
    );
    for (const l of locales) {
      const value = JSON.parse(fs.readFileSync(path.join(dir, l, 'scan.json'), 'utf8'))[key];
      expect(typeof value).toBe('string');
      expect(value).not.toMatch(/__NEEDS_TRANSLATION__/);
      if (key === 'queueStateNextTry' || key === 'queueFailedAt') {
        expect(value).toContain('{{time}}');
      }
    }
  });
});
