/**
 * #766: default timeout, retry with backoff and Retry-After, the never-retry
 * rules, 401 staying on the refresh path, and one Sentry report per request.
 */
const mockCapture = jest.fn();
const mockGetItem = jest.fn();
const mockSetItem = jest.fn();
const mockDeleteItem = jest.fn();
const mockAxiosPost = jest.fn();

jest.mock('../../lib/storage', () => ({
  getItem: (...a: unknown[]) => mockGetItem(...a),
  setItem: (...a: unknown[]) => mockSetItem(...a),
  deleteItem: (...a: unknown[]) => mockDeleteItem(...a),
}));

jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureException: (...a: unknown[]) => mockCapture(...a) },
  initSentry: jest.fn(),
}));

jest.mock('axios', () => {
  const savedRS = globalThis.ReadableStream;
  // @ts-expect-error avoid axios fetch adapter probe conflict
  delete globalThis.ReadableStream;
  const actual = jest.requireActual('axios');
  globalThis.ReadableStream = savedRS;
  return { ...actual, post: (...a: unknown[]) => mockAxiosPost(...a), create: actual.create };
});

import {
  api,
  configureRetry,
  gateStoredSession,
  resumeStoredSession,
  DEFAULT_TIMEOUT_MS,
  MAX_RETRIES,
  SCAN_UPLOAD_TIMEOUT_MS,
  SEARCH_TIMEOUT_MS,
  setAuthFailureCallback,
} from '../../lib/api';
import { MAX_RETRY_AFTER_MS, toApiError } from '../../lib/apiError';
import { resetApiErrorReporting } from '../../lib/apiErrorReporting';

type Step = {
  timeout?: boolean;
  status?: number;
  headers?: Record<string, string>;
  data?: unknown;
  ok?: boolean;
};

/** Adapter that plays `steps` in order (the last repeats) and counts calls. */
function script(steps: Step[]) {
  const calls: { method?: string; url?: string; auth?: unknown }[] = [];
  api.defaults.adapter = async (config) => {
    calls.push({ method: config.method, url: config.url, auth: config.headers?.Authorization });
    const step = steps[Math.min(calls.length - 1, steps.length - 1)];
    if (step.ok) {
      return { status: 200, data: { ok: true }, headers: {}, config, statusText: 'OK' };
    }
    if (step.timeout) {
      throw Object.assign(new Error('timeout exceeded'), {
        isAxiosError: true,
        config,
        code: 'ECONNABORTED',
        request: {},
      });
    }
    if (step.status === undefined) {
      throw Object.assign(new Error('Network Error'), { isAxiosError: true, config, request: {} });
    }
    const response = {
      status: step.status,
      data: step.data ?? {},
      headers: step.headers ?? {},
      config,
    };
    throw Object.assign(new Error(`Request failed with status code ${step.status}`), {
      isAxiosError: true,
      config,
      response,
    });
  };
  return calls;
}

const sleeps: number[] = [];

beforeEach(() => {
  jest.clearAllMocks();
  sleeps.length = 0;
  resetApiErrorReporting();
  mockGetItem.mockResolvedValue(null);
  setAuthFailureCallback(jest.fn());
  configureRetry({
    sleep: async (ms) => {
      sleeps.push(ms);
    },
    random: () => 0,
  });
});

afterAll(() => configureRetry());

describe('timeouts', () => {
  it('sets a default timeout on the instance, above the slowest normal endpoint', () => {
    expect(api.defaults.timeout).toBe(DEFAULT_TIMEOUT_MS);
    expect(DEFAULT_TIMEOUT_MS).toBeGreaterThanOrEqual(30_000);
  });

  it('gives search a longer timeout than the server worst case (10 s + 20 s)', () => {
    expect(SEARCH_TIMEOUT_MS).toBeGreaterThan(DEFAULT_TIMEOUT_MS);
    expect(SEARCH_TIMEOUT_MS).toBeGreaterThanOrEqual(40_000);
  });

  it('scan upload timeout covers the AI bound (120 s) + enrichment (20 s)', () => {
    expect(SCAN_UPLOAD_TIMEOUT_MS).toBeGreaterThanOrEqual(140_000);
  });

  it('search is sent with the search timeout', async () => {
    let seen: number | undefined;
    api.defaults.adapter = async (config) => {
      seen = config.timeout;
      return { status: 200, data: [], headers: {}, config, statusText: 'OK' };
    };
    await api.get('/books/search', { params: { q: 'x' } });
    expect(seen).toBe(SEARCH_TIMEOUT_MS);
  });

  it.each([
    ['post', '/scan', SCAN_UPLOAD_TIMEOUT_MS],
    ['get', '/user-books', DEFAULT_TIMEOUT_MS],
  ] as const)('%s %s gets its path timeout', async (method, url, expected) => {
    let seen: number | undefined;
    api.defaults.adapter = async (config) => {
      seen = config.timeout;
      return { status: 200, data: {}, headers: {}, config, statusText: 'OK' };
    };
    await api[method](url);
    expect(seen).toBe(expected);
  });

  it('a caller-set timeout wins over the path timeout', async () => {
    let seen: number | undefined;
    api.defaults.adapter = async (config) => {
      seen = config.timeout;
      return { status: 200, data: [], headers: {}, config, statusText: 'OK' };
    };
    await api.get('/books/search', { timeout: 5_000 });
    expect(seen).toBe(5_000);
  });
});

describe('retry', () => {
  it('retries a network error with exponential backoff, then succeeds', async () => {
    const calls = script([{}, {}, { ok: true }]);
    await expect(api.get('/user-books')).resolves.toMatchObject({ status: 200 });
    expect(calls).toHaveLength(3);
    expect(sleeps).toEqual([1000, 2000]);
  });

  it('does not retry a timeout (the server may still be working)', async () => {
    const calls = script([{ timeout: true }, { ok: true }]);
    await expect(api.get('/books/search', { timeout: SEARCH_TIMEOUT_MS })).rejects.toMatchObject({
      code: 'ECONNABORTED',
    });
    expect(calls).toHaveLength(1);
    expect(sleeps).toEqual([]);
  });

  it('gives up after MAX_RETRIES retries and rejects with the last error', async () => {
    const calls = script([{}]);
    await expect(api.get('/user-books')).rejects.toMatchObject({ message: 'Network Error' });
    expect(calls).toHaveLength(MAX_RETRIES + 1);
    expect(sleeps).toHaveLength(MAX_RETRIES);
  });

  it('adds jitter to the backoff', async () => {
    configureRetry({
      sleep: async (ms) => {
        sleeps.push(ms);
      },
      random: () => 1,
    });
    script([{}, { ok: true }]);
    await api.get('/user-books');
    expect(sleeps).toEqual([1250]);
  });

  it.each([408, 429, 502, 503, 504])('retries a GET that got %i', async (status) => {
    const calls = script([{ status }, { ok: true }]);
    await expect(api.get('/user-books')).resolves.toBeTruthy();
    expect(calls).toHaveLength(2);
  });

  it.each([400, 404, 409, 422, 500])('does not retry %i', async (status) => {
    const calls = script([{ status }, { ok: true }]);
    await expect(api.get('/user-books')).rejects.toBeTruthy();
    expect(calls).toHaveLength(1);
  });

  it.each(['put', 'delete', 'head', 'options'] as const)(
    'retries idempotent %s',
    async (method) => {
      const calls = script([{ status: 503 }, { ok: true }]);
      await expect(api.request({ url: '/user-books/x', method })).resolves.toBeTruthy();
      expect(calls).toHaveLength(2);
    }
  );

  it.each(['post', 'patch'] as const)('does not retry %s (#685)', async (method) => {
    const calls = script([{ status: 503 }, { ok: true }]);
    await expect(api.request({ url: '/user-books', method })).rejects.toBeTruthy();
    expect(calls).toHaveLength(1);
    expect(sleeps).toEqual([]);
  });

  it('never retries /scan, even a retryable status or a network error', async () => {
    let calls = script([{ status: 503 }, { ok: true }]);
    await expect(api.request({ url: '/scan', method: 'put' })).rejects.toBeTruthy();
    expect(calls).toHaveLength(1);
    calls = script([{}, { ok: true }]);
    await expect(api.post('/scan', {})).rejects.toBeTruthy();
    expect(calls).toHaveLength(1);
  });

  it('does not retry a canceled request', async () => {
    const calls = script([{}, { ok: true }]);
    const controller = new AbortController();
    controller.abort();
    await expect(api.get('/user-books', { signal: controller.signal })).rejects.toBeTruthy();
    expect(calls.length).toBeLessThanOrEqual(1);
    expect(sleeps).toEqual([]);
  });
});

describe('Retry-After', () => {
  it('honours seconds', async () => {
    script([{ status: 429, headers: { 'retry-after': '3' } }, { ok: true }]);
    await api.get('/user-books');
    expect(sleeps).toEqual([3000]);
  });

  it('honours an HTTP-date', async () => {
    const realNow = Date.now;
    Date.now = () => new Date('2030-01-01T00:00:00Z').getTime();
    try {
      script([
        { status: 503, headers: { 'retry-after': 'Tue, 01 Jan 2030 00:00:07 GMT' } },
        { ok: true },
      ]);
      await api.get('/user-books');
      expect(sleeps).toEqual([7000]);
    } finally {
      Date.now = realNow;
    }
  });

  it('caps the wait', async () => {
    script([{ status: 429, headers: { 'retry-after': '3600' } }, { ok: true }]);
    await api.get('/user-books');
    expect(sleeps).toEqual([MAX_RETRY_AFTER_MS]);
  });

  it('falls back to backoff when the header is unusable', async () => {
    script([{ status: 503, headers: { 'retry-after': 'soon' } }, { ok: true }]);
    await api.get('/user-books');
    expect(sleeps).toEqual([1000]);
  });
});

describe('session change during the retry wait', () => {
  it.each([
    ['sign-out', () => gateStoredSession(Promise.resolve(false))],
    ['new sign-in', () => resumeStoredSession()],
  ])('does not replay after a %s, and does not report', async (_name, changeSession) => {
    mockGetItem.mockResolvedValue('OLD');
    const calls = script([{ status: 503, headers: { 'retry-after': '5' } }, { ok: true }]);
    configureRetry({
      sleep: async (ms) => {
        sleeps.push(ms);
        changeSession();
      },
      random: () => 0,
    });
    await expect(api.get('/user-books')).rejects.toMatchObject({ response: { status: 503 } });
    expect(calls).toHaveLength(1);
    expect(sleeps).toEqual([5000]);
    expect(mockCapture).not.toHaveBeenCalled();
    resumeStoredSession();
  });

  it('a request started before an account switch never goes out with the new token', async () => {
    mockGetItem.mockResolvedValue('OLD');
    const calls = script([{ status: 503 }, { ok: true }]);
    configureRetry({
      sleep: async () => {
        mockGetItem.mockResolvedValue('NEW');
        resumeStoredSession();
      },
      random: () => 0,
    });
    await expect(api.get('/user-books')).rejects.toBeTruthy();
    expect(calls.map((c) => c.auth)).toEqual(['Bearer OLD']);
  });
});

describe('401', () => {
  it('goes through the refresh path, not the retry path', async () => {
    mockGetItem.mockImplementation(async (key: string) =>
      key === 'bookshelf_refresh_token' ? 'refresh' : 'old'
    );
    mockAxiosPost.mockResolvedValue({ data: { access_token: 'new', refresh_token: 'r2' } });
    const calls = script([{ status: 401 }, { ok: true }]);
    await expect(api.get('/user-books')).resolves.toBeTruthy();
    expect(calls).toHaveLength(2);
    expect(mockAxiosPost).toHaveBeenCalledTimes(1);
    expect(sleeps).toEqual([]);
  });

  it('a failed refresh does not fall into the retry loop', async () => {
    mockGetItem.mockResolvedValue(null);
    const calls = script([{ status: 401 }, { ok: true }]);
    await expect(api.get('/user-books')).rejects.toBeTruthy();
    expect(calls).toHaveLength(1);
    expect(sleeps).toEqual([]);
  });
});

describe('Sentry', () => {
  it('reports a persistent 503 once, not once per attempt', async () => {
    const calls = script([{ status: 503 }]);
    await expect(api.get('/user-books')).rejects.toBeTruthy();
    expect(calls).toHaveLength(MAX_RETRIES + 1);
    expect(mockCapture).toHaveBeenCalledTimes(1);
  });

  it('does not report a failure that a retry recovered from', async () => {
    script([{ status: 503 }, { ok: true }]);
    await api.get('/user-books');
    expect(mockCapture).not.toHaveBeenCalled();
  });
});

describe('toApiError', () => {
  it('reads a string detail, the status and Retry-After', () => {
    const err = toApiError({
      message: 'x',
      response: {
        status: 429,
        data: { detail: 'daily_scan_limit' },
        headers: { 'Retry-After': '12' },
      },
    });
    expect(err).toMatchObject({
      status: 429,
      code: 'daily_scan_limit',
      retryAfterMs: 12000,
      isNetworkError: false,
      isTimeout: false,
    });
  });

  it('reads detail.code and detail.detail.code', () => {
    expect(toApiError({ response: { status: 409, data: { detail: { code: 'a' } } } }).code).toBe(
      'a'
    );
    expect(
      toApiError({ response: { status: 409, data: { detail: { detail: { code: 'b' } } } } }).code
    ).toBe('b');
  });

  it('has no code for a validation error list', () => {
    expect(toApiError({ response: { status: 422, data: { detail: [{ msg: 'x' }] } } }).code).toBe(
      undefined
    );
  });

  it('flags network errors and timeouts', () => {
    const net = toApiError({ message: 'Network Error' });
    expect(net).toMatchObject({ isNetworkError: true, isTimeout: false });
    expect(net.status).toBeUndefined();
    expect(
      toApiError({ code: 'ECONNABORTED', message: 'timeout of 15000ms exceeded' })
    ).toMatchObject({ isNetworkError: true, isTimeout: true });
  });

  it('never throws on junk', () => {
    expect(toApiError(null).isNetworkError).toBe(true);
    expect(toApiError('boom').isNetworkError).toBe(true);
  });
});
