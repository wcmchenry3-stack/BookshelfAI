import fs from 'fs';
import path from 'path';
import React from 'react';
import { Alert, StyleSheet } from 'react-native';
import { act, fireEvent, render, renderHook, waitFor } from '@testing-library/react-native';

import ScanQueueScreen from '../../app/scan-queue';
import { BannerProvider } from '../../contexts/BannerContext';
import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { useBanner } from '../../hooks/useBanner';
import { useScanJobs } from '../../hooks/useScanJobs';
import { SCAN_RETRY_DELAYS_MS } from '../../lib/scanErrors';
import { leaveScanQueue } from '../../lib/scanQueueNav';
import { FAILED_SCAN_RETENTION_MS, isFailedScanExpired, type ScanJob } from '../../lib/scanJob';

// #605: the scan queue screen (waiting + failed photos, retry / delete) and
// the 7-day expiry of failed scans.

jest.mock('@expo/vector-icons', () => ({ MaterialIcons: () => null }));

const mockPost = jest.fn();
jest.mock('../../lib/api', () => ({
  api: { post: (...a: unknown[]) => mockPost(...a), get: jest.fn() },
}));

const mockSaveJobs = jest.fn();
const mockLoadJobs = jest.fn();
const mockDeleteImage = jest.fn();
const mockSweep = jest.fn();
jest.mock('../../lib/scanJobStorage', () => ({
  loadJobs: () => mockLoadJobs(),
  saveJobs: (...a: unknown[]) => mockSaveJobs(...a),
  clearJobs: jest.fn(),
  sweepOrphanedScanFiles: (...a: unknown[]) => mockSweep(...a),
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
const mockBack = jest.fn();
const mockReplace = jest.fn();
let mockCanGoBack = true;
jest.mock('expo-router', () => ({
  router: {
    push: (...a: unknown[]) => mockPush(...a),
    back: (...a: unknown[]) => mockBack(...a),
    replace: (...a: unknown[]) => mockReplace(...a),
    canGoBack: () => mockCanGoBack,
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
  mockSweep.mockResolvedValue(0);
  mockCanGoBack = true;
});

afterEach(() => {
  jest.restoreAllMocks();
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
    expect(getAllByLabelText(/^Retry scan from /)).toHaveLength(1);
    expect(getAllByLabelText(/^Delete scan from /)).toHaveLength(1);
    expect(queryByText('No failed scans.')).toBeNull();
    expect(queryByText('No photos are waiting.')).toBeNull();
  });

  it('Retry restarts the photo at attempt 1 and sends it', async () => {
    mockPost.mockRejectedValue({ response: { status: 503, data: { detail: 'scan_unavailable' } } });
    const { getByLabelText, getByTestId } = await renderScreen([
      job('bad', { attempts: 5, retryCount: 2, failedAt: NOW - 1000 }),
    ]);
    await act(async () => {
      fireEvent.press(getByLabelText(/^Retry scan from /));
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

  describe('Delete', () => {
    it('asks first; nothing is removed until Delete is confirmed', async () => {
      const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
      const { getByLabelText, queryByTestId, getByText } = await renderScreen([job('bad')]);
      await act(async () => {
        fireEvent.press(getByLabelText(/^Delete scan from /));
      });
      expect(alertSpy).toHaveBeenCalledTimes(1);
      const [title, , buttons] = alertSpy.mock.calls[0];
      expect(title).toBe('Delete this scan? The photo will be removed.');
      expect(buttons!.map((b) => [b.text, b.style])).toEqual([
        ['Cancel', 'cancel'],
        ['Delete', 'destructive'],
      ]);
      expect(mockDeleteImage).not.toHaveBeenCalled();
      expect(queryByTestId('queue-failed-bad')).toBeTruthy();

      await act(async () => {
        buttons![1].onPress!();
      });
      expect(mockDeleteImage).toHaveBeenCalledWith('file:///docs/scan-queue/bad.jpg');
      expect(queryByTestId('queue-failed-bad')).toBeNull();
      expect(getByText('No failed scans.')).toBeTruthy();
      alertSpy.mockRestore();
    });

    it('Cancel keeps the scan', async () => {
      const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
      const { getByLabelText, queryByTestId } = await renderScreen([job('bad')]);
      await act(async () => {
        fireEvent.press(getByLabelText(/^Delete scan from /));
      });
      const buttons = alertSpy.mock.calls[0][2]!;
      await act(async () => {
        buttons[0].onPress?.();
      });
      expect(mockDeleteImage).not.toHaveBeenCalled();
      expect(queryByTestId('queue-failed-bad')).toBeTruthy();
      alertSpy.mockRestore();
    });
  });

  it('buttons are accessible buttons of at least 44 x 44 pt, with space between them', async () => {
    const { getByLabelText, getByTestId } = await renderScreen([job('bad')]);
    for (const label of [/^Retry scan from /, /^Delete scan from /]) {
      const el = getByLabelText(label);
      expect(el.props.accessibilityRole).toBe('button');
      const style = StyleSheet.flatten(el.props.style);
      expect(style.minHeight).toBeGreaterThanOrEqual(44);
      expect(style.minWidth).toBeGreaterThanOrEqual(44);
    }
    const actions = StyleSheet.flatten(getByTestId('queue-actions-bad').props.style);
    expect(actions.gap).toBeGreaterThanOrEqual(16);
  });

  it('gives each row distinct button labels and reads the thumbnail and state as one item', async () => {
    const { getByLabelText, getAllByLabelText, getByTestId } = await renderScreen([
      job('a', { failedAt: NOW - 3600_000 }),
      job('b', { failedAt: NOW - 7200_000 }),
      job('text', { status: 'queued', type: 'text', imageUri: undefined, query: 'Dune' }),
    ]);
    const retry = getAllByLabelText(/^Retry scan from /).map((e) => e.props.accessibilityLabel);
    const del = getAllByLabelText(/^Delete scan from /).map((e) => e.props.accessibilityLabel);
    expect(new Set(retry).size).toBe(2);
    expect(new Set(del).size).toBe(2);
    expect(getAllByLabelText(/^Scan photo: Failed /)).toHaveLength(2);
    expect(getByLabelText('Dune: Waiting for connection').props.accessible).toBe(true);
    // The photo itself is decorative.
    const thumb = getByTestId('queue-thumb-a');
    expect(thumb.props.accessible).toBe(false);
    expect(thumb.props.accessibilityRole).toBeUndefined();
  });

  it('shows a neutral placeholder when the photo file is missing', async () => {
    const { getByTestId, queryByTestId } = await renderScreen([job('gone')]);
    expect(queryByTestId('queue-thumb-missing-gone')).toBeNull();
    await act(async () => {
      fireEvent(getByTestId('queue-thumb-gone'), 'error');
    });
    expect(getByTestId('queue-thumb-missing-gone')).toBeTruthy();
    expect(queryByTestId('queue-thumb-gone')).toBeNull();
  });

  it('refreshes "Next try at" every 30 s and clears the timer on unmount', async () => {
    const jobs = [job('w', { status: 'queued', attempts: 1, nextAttemptAt: NOW + 40_000 })];
    jest.spyOn(require('../../hooks/useScanJobs'), 'useScanJobs').mockReturnValue({
      jobs,
      dismissJob: jest.fn(),
      restartScanSchedule: jest.fn(),
      purgeExpiredFailedJobs: jest.fn(),
    });
    const clearSpy = jest.spyOn(global, 'clearInterval');
    const { getByText, unmount } = await render(<ScanQueueScreen />);
    expect(getByText(/^Next try at /)).toBeTruthy();
    await act(async () => {
      await jest.advanceTimersByTimeAsync(60_000);
    });
    expect(getByText('Waiting for connection')).toBeTruthy();
    clearSpy.mockClear();
    await act(async () => {
      unmount();
    });
    expect(clearSpy).toHaveBeenCalledTimes(1);
  });

  it('Back goes to the home tab when there is nothing to go back to', () => {
    mockCanGoBack = true;
    leaveScanQueue();
    expect(mockBack).toHaveBeenCalledTimes(1);
    mockCanGoBack = false;
    leaveScanQueue();
    expect(mockReplace).toHaveBeenCalledWith('/');
    expect(mockBack).toHaveBeenCalledTimes(1);
  });
});

describe('Retry respects one photo at a time (#604)', () => {
  const okBody = { data: { books: [] } };

  async function setup(persisted: ScanJob[]) {
    mockLoadJobs.mockResolvedValue(persisted);
    const hook = await renderHook(() => useScanJobs(), { wrapper });
    await waitFor(() => expect(mockSaveJobs).toHaveBeenCalled());
    return hook;
  }

  it('does not send a retried photo while another photo is sending; sends it afterwards', async () => {
    let finishFirst!: (v: unknown) => void;
    mockPost.mockReturnValueOnce(new Promise((r) => (finishFirst = r)));
    mockPost.mockResolvedValue(okBody);
    const hook = await setup([job('bad', { failedAt: NOW - 1000, attempts: 5, retryCount: 2 })]);

    await act(async () => {
      void hook.result.current.startScan('image', 'file:///docs/scan-queue/first.jpg');
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(mockPost).toHaveBeenCalledTimes(1);

    await act(async () => {
      await hook.result.current.restartScanSchedule('bad');
      await jest.advanceTimersByTimeAsync(0);
    });
    // Not sent concurrently; waiting its turn with a clean schedule.
    expect(mockPost).toHaveBeenCalledTimes(1);
    const bad = hook.result.current.jobs.find((j) => j.id === 'bad')!;
    expect(bad).toEqual(expect.objectContaining({ status: 'queued', retryCount: 0 }));
    expect(bad.attempts).toBeUndefined();
    expect(bad.nextAttemptAt).toBeUndefined();
    expect(bad.failedAt).toBeUndefined();

    await act(async () => {
      finishFirst(okBody);
      await jest.advanceTimersByTimeAsync(5000);
    });
    expect(mockPost).toHaveBeenCalledTimes(2);
  });

  it('sends a retried photo now when nothing else is going on', async () => {
    mockPost.mockResolvedValue(okBody);
    const hook = await setup([job('bad', { failedAt: NOW - 1000 })]);
    await act(async () => {
      await hook.result.current.restartScanSchedule('bad');
    });
    expect(mockPost).toHaveBeenCalledTimes(1);
  });

  it('queues a retried photo behind a photo waiting on its retry schedule', async () => {
    mockPost.mockResolvedValue(okBody);
    const hook = await setup([
      job('bad', { failedAt: NOW - 1000 }),
      job('waits', { status: 'queued', attempts: 1, nextAttemptAt: NOW + 600_000 }),
    ]);
    await act(async () => {
      await hook.result.current.restartScanSchedule('bad');
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(mockPost).not.toHaveBeenCalled();
    expect(hook.result.current.jobs.find((j) => j.id === 'bad')!.status).toBe('queued');
  });

  it('Retry clears failedAt', async () => {
    mockPost.mockReturnValue(new Promise(() => {}));
    const hook = await setup([job('bad', { failedAt: NOW - 1000 })]);
    await act(async () => {
      void hook.result.current.restartScanSchedule('bad');
      await jest.advanceTimersByTimeAsync(0);
    });
    const bad = hook.result.current.jobs.find((j) => j.id === 'bad')!;
    expect(bad.status).toBe('searching');
    expect(bad.failedAt).toBeUndefined();
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

  it('drops old failed jobs on load and leaves their images to the sweep; counts from createdAt without failedAt', async () => {
    mockLoadJobs.mockResolvedValue([
      job('old', { failedAt: NOW - 8 * DAY }),
      job('legacy', { createdAt: NOW - 9 * DAY }),
      job('fresh', { failedAt: NOW - 6 * DAY }),
      job('waiting', { status: 'queued', createdAt: NOW - 30 * DAY }),
    ]);
    const { result } = await renderHook(() => useScanJobs(), { wrapper });
    await waitFor(() => expect(result.current.jobs.length).toBe(2));
    expect(result.current.jobs.map((j) => j.id)).toEqual(['fresh', 'waiting']);
    // No explicit delete before the pruned list is saved: the sweep removes
    // the files that no kept job refers to.
    expect(mockDeleteImage).not.toHaveBeenCalled();
    await waitFor(() => expect(mockSweep).toHaveBeenCalledTimes(1));
    const [kept, cutoff] = mockSweep.mock.calls[0] as [ScanJob[], number];
    expect(kept.map((j) => j.id)).toEqual(['fresh', 'waiting']);
    // The purged files were captured before launch, so the sweep's age guard allows them.
    expect(cutoff).toBe(NOW);
    expect(NOW - 8 * DAY).toBeLessThan(cutoff);
  });

  it('the on-open purge saves the pruned list before deleting images', async () => {
    mockLoadJobs.mockResolvedValue([
      job('soon', { failedAt: NOW - 6 * DAY }),
      job('queued', { status: 'queued', createdAt: NOW - 30 * DAY }),
    ]);
    const hook = await renderHook(() => useScanJobs(), { wrapper });
    await waitFor(() => expect(hook.result.current.jobs.length).toBe(2));
    const events: string[] = [];
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    mockSaveJobs.mockImplementation(async (list: ScanJob[]) => {
      events.push(`save:${list.map((j) => j.id).join(',')}`);
      await gate;
    });
    mockDeleteImage.mockImplementation(() => events.push('delete'));

    jest.setSystemTime(NOW + 2 * DAY);
    await act(async () => {
      hook.result.current.purgeExpiredFailedJobs();
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(events).toContain('save:queued');
    expect(events).not.toContain('delete');
    await act(async () => {
      release();
      await jest.advanceTimersByTimeAsync(0);
    });
    expect(events.indexOf('delete')).toBeGreaterThan(events.indexOf('save:queued'));
    expect(mockDeleteImage).toHaveBeenCalledTimes(1);
    expect(mockDeleteImage).toHaveBeenCalledWith('file:///docs/scan-queue/soon.jpg');
  });

  it('the purge leaves pending, queued and in-flight jobs alone, however old', async () => {
    mockPost.mockReturnValue(new Promise(() => {}));
    mockLoadJobs.mockResolvedValue([
      job('queued', { status: 'queued', createdAt: NOW - 30 * DAY }),
      job('pending', { status: 'pending', createdAt: NOW - 30 * DAY }),
    ]);
    const hook = await renderHook(() => useScanJobs(), { wrapper });
    await waitFor(() => expect(hook.result.current.jobs.length).toBe(2));
    await act(async () => {
      void hook.result.current.startScan('image', 'file:///docs/scan-queue/live.jpg');
      await jest.advanceTimersByTimeAsync(0);
    });
    jest.setSystemTime(NOW + 30 * DAY);
    await act(async () => {
      hook.result.current.purgeExpiredFailedJobs();
    });
    expect(hook.result.current.jobs.map((j) => j.id)).toEqual(
      expect.arrayContaining(['queued', 'pending'])
    );
    expect(hook.result.current.jobs).toHaveLength(3);
    expect(hook.result.current.jobs.some((j) => j.status === 'searching')).toBe(true);
    expect(mockDeleteImage).not.toHaveBeenCalled();
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
    'queueDeleteConfirm',
    'queueDeleteCancel',
    'queueRetryA11y',
    'queueDeleteA11y',
    'queueRowA11y',
    'viewQueueFailed',
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
      expect(value.length).toBeLessThanOrEqual(meta[key].characterLimit);
      if (
        ['queueStateNextTry', 'queueFailedAt', 'queueRetryA11y', 'queueDeleteA11y'].includes(key)
      ) {
        expect(value).toContain('{{time}}');
      }
      if (key === 'queueRowA11y') {
        expect(value).toContain('{{what}}');
        expect(value).toContain('{{state}}');
      }
      if (key === 'viewQueueFailed') expect(value).toContain('{{count}}');
    }
  });
});
