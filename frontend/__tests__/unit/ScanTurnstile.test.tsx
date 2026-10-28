import React from 'react';
import { act, renderHook, screen, waitFor } from '@testing-library/react-native';

import { BannerProvider } from '../../contexts/BannerContext';
import { ScanJobProvider } from '../../contexts/ScanJobContext';
import { resetTurnstileLoggingForTests, TurnstileProvider } from '../../contexts/TurnstileContext';
import { useBanner } from '../../hooks/useBanner';
import { useScanJobs } from '../../hooks/useScanJobs';
import { MAX_SCAN_ATTEMPTS } from '../../lib/scanErrors';

// #740: every POST /scan attempt carries a fresh Cloudflare Turnstile token
// from the hidden WebView widget, as the `cf-turnstile-response` form field.

const mockPost = jest.fn();
jest.mock('../../lib/api', () => ({
  api: { post: (...a: unknown[]) => mockPost(...a), get: jest.fn() },
}));

let mockAuthed = true;
jest.mock('../../hooks/useAuth', () => ({ useAuth: () => ({ isAuthenticated: mockAuthed }) }));

const mockSaveJobs = jest.fn();
const mockLoadJobs = jest.fn();
jest.mock('../../lib/scanJobStorage', () => ({
  loadJobs: () => mockLoadJobs(),
  saveJobs: (...a: unknown[]) => mockSaveJobs(...a),
  clearJobs: () => Promise.resolve(),
  sweepOrphanedScanFiles: () => Promise.resolve(0),
  deleteScanImage: () => Promise.resolve(),
}));

const mockNetFetch = jest.fn();
jest.mock('@react-native-community/netinfo', () => ({
  __esModule: true,
  default: { fetch: () => mockNetFetch(), addEventListener: () => () => {} },
}));

const mockBreadcrumb = jest.fn();
jest.mock('../../lib/sentry', () => ({
  Sentry: {
    addBreadcrumb: (...a: unknown[]) => mockBreadcrumb(...a),
    captureException: jest.fn(),
    captureMessage: jest.fn(),
  },
}));

let mockUuid = 0;
jest.mock('expo-crypto', () => ({ randomUUID: () => `mockUuid-${++mockUuid}` }));

const { __mock: webView } = jest.requireMock('react-native-webview') as {
  __mock: { injectJavaScript: jest.Mock; reload: jest.Mock };
};

const BASE = 'https://bookshelfapi.example.com';
const SITE_KEY = '1x00000000000000000000AA';

function wrapper({ children }: { children: React.ReactNode }) {
  return (
    <BannerProvider>
      <TurnstileProvider>
        <ScanJobProvider>{children}</ScanJobProvider>
      </TurnstileProvider>
    </BannerProvider>
  );
}

/** Post a message from the inline page, as react-native-webview would. */
function fromPage(msg: object, url = BASE) {
  const el = screen.getByTestId('turnstile-webview', { includeHiddenElements: true });
  el.props.onMessage({ nativeEvent: { data: JSON.stringify(msg), url } });
}

/** The request ids the app asked the page to run a challenge for. */
function challengeIds(): number[] {
  return webView.injectJavaScript.mock.calls
    .map(([js]) => /\.run\((\d+)\)/.exec(js as string))
    .filter((m): m is RegExpExecArray => m !== null)
    .map((m) => Number(m[1]));
}

/** The page answers each challenge with `tok-<id>`. */
function autoSolve() {
  webView.injectJavaScript.mockImplementation((js: string) => {
    const m = /\.run\((\d+)\)/.exec(js);
    if (m) {
      const id = Number(m[1]);
      Promise.resolve().then(() => fromPage({ type: 'token', id, token: `tok-${id}` }));
    }
  });
}

/** A form field of the n-th POST /scan (undefined when absent). */
function fieldOf(n: number, name: string): unknown {
  const form = mockPost.mock.calls[n][1] as FormData & { _parts?: [string, unknown][] };
  if (typeof form.get === 'function') return form.get(name) ?? undefined;
  return Object.fromEntries(form._parts ?? [])[name];
}
/** The `cf-turnstile-response` of every POST /scan, in order (undefined: none). */
function tokensSent(): unknown[] {
  return mockPost.mock.calls.map((_c, i) => fieldOf(i, 'cf-turnstile-response'));
}

function refusal(status: number, detail: string, headers = {}) {
  return { response: { status, data: { detail }, headers } };
}
const TURNSTILE_FAILED = refusal(403, 'Turnstile verification failed');
const TURNSTILE_MISSING = refusal(400, 'Missing Turnstile token');
const OK = {
  data: { books: [{ title: 'Dune', author: 'Herbert' }], enhanced_scans_remaining: null },
};

async function setup({ ready = true } = {}) {
  const hook = await renderHook(() => ({ scan: useScanJobs(), banner: useBanner() }), {
    wrapper,
  });
  await waitFor(() => expect(mockSaveJobs).toHaveBeenCalled());
  if (ready) fromPage({ type: 'ready' });
  return hook;
}

async function advance(ms: number) {
  await act(async () => {
    await jest.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  jest.resetAllMocks();
  jest.useFakeTimers();
  mockUuid = 0;
  mockAuthed = true;
  resetTurnstileLoggingForTests();
  process.env.EXPO_PUBLIC_TURNSTILE_SITE_KEY = SITE_KEY;
  process.env.EXPO_PUBLIC_TURNSTILE_BASE_URL = BASE;
  mockSaveJobs.mockResolvedValue(undefined);
  mockLoadJobs.mockResolvedValue([]);
  mockNetFetch.mockResolvedValue({ isConnected: true });
});

afterEach(() => {
  jest.useRealTimers();
  delete process.env.EXPO_PUBLIC_TURNSTILE_SITE_KEY;
  delete process.env.EXPO_PUBLIC_TURNSTILE_BASE_URL;
});

describe('POST /scan carries a Turnstile token (#740)', () => {
  it('appends the token the WebView posts back as cf-turnstile-response', async () => {
    autoSolve();
    mockPost.mockResolvedValue(OK);
    const { result } = await setup();
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///a.jpg');
    });
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost.mock.calls[0][0]).toBe('/scan');
    expect(tokensSent()).toEqual(['tok-1']);
    // The other fields are unchanged.
    expect(fieldOf(0, 'attempt')).toBe('1');
    expect(fieldOf(0, 'file')).toBeDefined();
    expect(result.current.scan.jobs[0].status).toBe('complete');
  });

  it('runs a fresh challenge for every scan', async () => {
    autoSolve();
    mockPost.mockResolvedValue(OK);
    const { result } = await setup();
    for (const uri of ['file:///a.jpg', 'file:///b.jpg', 'file:///c.jpg']) {
      await act(async () => {
        await result.current.scan.startScan('image', uri);
      });
    }
    expect(tokensSent()).toEqual(['tok-1', 'tok-2', 'tok-3']);
    expect(challengeIds()).toEqual([1, 2, 3]);
  });

  it('gets a new token for the silent scan_in_progress retry', async () => {
    autoSolve();
    mockPost
      .mockRejectedValueOnce(refusal(429, 'scan_in_progress', { 'retry-after': '2' }))
      .mockResolvedValue(OK);
    const { result } = await setup();
    await act(async () => {
      void result.current.scan.startScan('image', 'file:///a.jpg');
    });
    await advance(2_000);
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));
    expect(tokensSent()).toEqual(['tok-1', 'tok-2']);
  });

  it('gets a new token when a queued photo is sent later', async () => {
    autoSolve();
    mockPost.mockResolvedValue(OK);
    mockLoadJobs.mockResolvedValue([
      {
        id: 'q-1',
        type: 'image',
        status: 'queued',
        createdAt: 1,
        imageUri: 'file:///q1.jpg',
        retryCount: 0,
      },
    ]);
    const { result } = await setup();
    // "Try again" sends the queue.
    await act(async () => {
      result.current.scan.resumeQueue();
    });
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///b.jpg');
    });
    expect(tokensSent()).toEqual(['tok-1', 'tok-2']);
  });
});

describe('a Turnstile refusal is retryable (#740)', () => {
  it.each([
    ['403 Turnstile verification failed', TURNSTILE_FAILED],
    ['400 Missing Turnstile token', TURNSTILE_MISSING],
  ])('%s: queued for the retry schedule, which sends a new token', async (_n, error) => {
    autoSolve();
    mockPost.mockRejectedValueOnce(error).mockResolvedValue(OK);
    const { result } = await setup();
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///a.jpg');
    });
    // Not a permanent failure: waiting for its next attempt.
    expect(result.current.scan.jobs[0]).toEqual(
      expect.objectContaining({ status: 'queued', error: 'unavailable', attempts: 1 })
    );
    expect(result.current.banner.banner?.type).not.toBe('error');

    await advance(30_000);
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(2));
    expect(tokensSent()).toEqual(['tok-1', 'tok-2']);
    expect(fieldOf(1, 'attempt')).toBe('2');
    await waitFor(() => expect(result.current.scan.jobs[0].status).toBe('complete'));
  });

  it('keeps the existing cap: failed after the last attempt, never retried forever', async () => {
    autoSolve();
    mockPost.mockRejectedValue(TURNSTILE_FAILED);
    const { result } = await setup();
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///a.jpg');
    });
    for (const delay of [30_000, 600_000, 3_600_000, 14_400_000]) await advance(delay);
    await waitFor(() => expect(result.current.scan.jobs[0].status).toBe('failed'));
    expect(mockPost).toHaveBeenCalledTimes(MAX_SCAN_ATTEMPTS);
    // Each attempt had its own token.
    expect(new Set(tokensSent()).size).toBe(MAX_SCAN_ATTEMPTS);
    await advance(24 * 3_600_000);
    expect(mockPost).toHaveBeenCalledTimes(MAX_SCAN_ATTEMPTS);
  });
});

describe('without a token the scan is still sent (#740)', () => {
  it('site key unset: no widget, no token, the scan proceeds; logged once', async () => {
    delete process.env.EXPO_PUBLIC_TURNSTILE_SITE_KEY;
    mockPost.mockResolvedValue(OK);
    const { result } = await setup({ ready: false });
    expect(screen.queryByTestId('turnstile-webview', { includeHiddenElements: true })).toBeNull();
    for (const uri of ['file:///a.jpg', 'file:///b.jpg']) {
      await act(async () => {
        await result.current.scan.startScan('image', uri);
      });
    }
    expect(mockPost).toHaveBeenCalledTimes(2);
    expect(tokensSent()).toEqual([undefined, undefined]);
    expect(result.current.scan.jobs.every((j) => j.status === 'complete')).toBe(true);
    const notConfigured = mockBreadcrumb.mock.calls.filter(([b]) =>
      String(b.message).startsWith('Turnstile not configured')
    );
    expect(notConfigured).toHaveLength(1);
  });

  it('times out after 10 s and sends the request without a token', async () => {
    // The page never answers.
    mockPost.mockResolvedValue(OK);
    const { result } = await setup();
    await act(async () => {
      void result.current.scan.startScan('image', 'file:///a.jpg');
    });
    expect(challengeIds()).toEqual([1]);
    await advance(9_999);
    expect(mockPost).not.toHaveBeenCalled();
    await advance(1);
    await waitFor(() => expect(mockPost).toHaveBeenCalledTimes(1));
    expect(tokensSent()).toEqual([undefined]);
    // The abandoned challenge was reset in the page.
    expect(webView.injectJavaScript).toHaveBeenLastCalledWith(expect.stringContaining('.reset()'));
    // A token arriving late is not kept for the next scan.
    fromPage({ type: 'token', id: 1, token: 'late' });
    autoSolve();
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///b.jpg');
    });
    expect(tokensSent()).toEqual([undefined, 'tok-2']);
  });

  it('a widget error sends the request without a token, at once', async () => {
    webView.injectJavaScript.mockImplementation((js: string) => {
      const m = /\.run\((\d+)\)/.exec(js);
      if (m) Promise.resolve().then(() => fromPage({ type: 'error', id: Number(m[1]), code: '1' }));
    });
    mockPost.mockResolvedValue(OK);
    const { result } = await setup();
    await act(async () => {
      await result.current.scan.startScan('image', 'file:///a.jpg');
    });
    expect(tokensSent()).toEqual([undefined]);
  });
});

describe('sign-out (#740 with #723)', () => {
  it('a token requested before sign-out is never used afterwards', async () => {
    // The page does not answer until told to.
    mockPost.mockResolvedValue(OK);
    const hook = await setup();
    await act(async () => {
      void hook.result.current.scan.startScan('image', 'file:///a.jpg');
    });
    expect(challengeIds()).toEqual([1]);

    mockAuthed = false;
    await act(async () => {
      hook.rerender({});
    });
    // The token for the signed-out request arrives now.
    await act(async () => {
      fromPage({ type: 'token', id: 1, token: 'tok-before-sign-out' });
      await jest.advanceTimersByTimeAsync(20_000);
    });
    expect(mockPost).not.toHaveBeenCalled();
    expect(hook.result.current.scan.jobs).toEqual([]);

    // The next account's scan runs its own challenge.
    mockAuthed = true;
    await act(async () => {
      hook.rerender({});
    });
    autoSolve();
    await act(async () => {
      await hook.result.current.scan.startScan('image', 'file:///b.jpg');
    });
    expect(tokensSent()).toEqual(['tok-2']);
  });
});
