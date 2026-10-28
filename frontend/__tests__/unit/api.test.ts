/**
 * Tests for the axios instance in lib/api.ts.
 *
 * We exercise the request interceptor (token injection) and the response
 * interceptor (401 → refresh → retry, and failure paths) without making
 * real HTTP calls.
 */

// The refresh-retry tests invoke api(original) which triggers the async
// request interceptor chain (token lookup + Sentry breadcrumb) before
// failing with a network error. This takes ~8s locally.
jest.setTimeout(15000);

const mockGetItem = jest.fn();
const mockSetItem = jest.fn();
const mockDeleteItem = jest.fn();

jest.mock('../../lib/storage', () => ({
  getItem: (...args: unknown[]) => mockGetItem(...args),
  setItem: (...args: unknown[]) => mockSetItem(...args),
  deleteItem: (...args: unknown[]) => mockDeleteItem(...args),
}));

jest.mock('../../lib/sentry', () => ({
  Sentry: {
    addBreadcrumb: jest.fn(),
    captureException: jest.fn(),
  },
  initSentry: jest.fn(),
}));

// Mock axios.post used in the refresh leg of the interceptor.
// We leave the axios instance itself intact so interceptors run normally.
const mockAxiosPost = jest.fn();
jest.mock('axios', () => {
  // Expo SDK 55's streams polyfill conflicts with axios's fetch adapter
  // probe at module-evaluation time. Temporarily remove ReadableStream so
  // axios falls back to the XHR adapter, then restore it.
  const savedRS = globalThis.ReadableStream;
  // @ts-expect-error intentional deletion to avoid fetch adapter conflict
  delete globalThis.ReadableStream;
  const actual = jest.requireActual('axios');
  globalThis.ReadableStream = savedRS;
  return {
    ...actual,
    post: (...args: unknown[]) => mockAxiosPost(...args),
    create: actual.create,
  };
});

import {
  ACCESS_TOKEN_KEY,
  api,
  REFRESH_TOKEN_KEY,
  setAuthFailureCallback,
  TOKEN_REVOKED,
} from '../../lib/api';

// ─── Helpers ─────────────────────────────────────────────────────────────────

function make401Error(config: object = {}) {
  return {
    response: { status: 401 },
    config: { headers: {}, ...config },
  };
}

function makeNetworkError() {
  return { request: {}, message: 'Network Error' }; // no .response
}

// Pull the interceptors out of the axios instance so we can invoke them directly
const requestHandlers = (api.interceptors.request as any).handlers;
const responseHandlers = (api.interceptors.response as any).handlers;

async function runRequestInterceptor(config: object = { headers: {} }) {
  const fulfilled = requestHandlers[0]?.fulfilled;
  return fulfilled(config);
}

async function runResponseErrorInterceptor(error: object) {
  const rejected = responseHandlers[0]?.rejected;
  return rejected(error);
}

// ─── Request interceptor — token injection ────────────────────────────────────

describe('api request interceptor', () => {
  beforeEach(() => jest.clearAllMocks());

  it('injects Authorization header when token exists', async () => {
    mockGetItem.mockResolvedValue('my-access-token');
    const config = { headers: {} as Record<string, string> };
    const result = await runRequestInterceptor(config);
    expect(result.headers.Authorization).toBe('Bearer my-access-token');
  });

  it('leaves Authorization header absent when no token', async () => {
    mockGetItem.mockResolvedValue(null);
    const config = { headers: {} as Record<string, string> };
    const result = await runRequestInterceptor(config);
    expect(result.headers.Authorization).toBeUndefined();
  });

  it('reads from ACCESS_TOKEN_KEY', async () => {
    mockGetItem.mockResolvedValue(null);
    await runRequestInterceptor({ headers: {} });
    expect(mockGetItem).toHaveBeenCalledWith(ACCESS_TOKEN_KEY);
  });
});

// ─── Response interceptor — 401 handling ─────────────────────────────────────

describe('api response interceptor — 401 handling', () => {
  beforeEach(() => jest.clearAllMocks());

  it('passes non-401 errors through unchanged', async () => {
    const err = { response: { status: 500 }, config: {} };
    await expect(runResponseErrorInterceptor(err)).rejects.toEqual(err);
  });

  it('passes network errors (no response) through unchanged', async () => {
    const err = makeNetworkError();
    await expect(runResponseErrorInterceptor(err)).rejects.toEqual(err);
  });

  it('does not retry when _retry flag already set', async () => {
    const err = make401Error({ _retry: true });
    await expect(runResponseErrorInterceptor(err)).rejects.toBeDefined();
    expect(mockAxiosPost).not.toHaveBeenCalled();
  });

  it('does not call refresh when no refresh token stored', async () => {
    mockGetItem.mockResolvedValue(null); // no refresh token
    const err = make401Error();
    await expect(runResponseErrorInterceptor(err)).rejects.toBeDefined();
    expect(mockAxiosPost).not.toHaveBeenCalled();
  });

  it('calls POST /auth/refresh with stored refresh token', async () => {
    mockGetItem.mockImplementation((key: string) =>
      key === REFRESH_TOKEN_KEY ? Promise.resolve('old-refresh') : Promise.resolve(null)
    );
    mockAxiosPost.mockResolvedValue({
      data: { access_token: 'new-access', refresh_token: 'new-refresh' },
    });

    // The interceptor calls api(original) to retry — that will fail with a
    // network error in Jest (no HTTP server). Absorb it; we only care that
    // the refresh POST and token storage happened first.
    await runResponseErrorInterceptor(make401Error()).catch(() => {});

    expect(mockAxiosPost).toHaveBeenCalledWith(expect.stringContaining('/auth/refresh'), {
      refresh_token: 'old-refresh',
    });
  });

  it('stores new tokens after successful refresh', async () => {
    mockGetItem.mockImplementation((key: string) =>
      key === REFRESH_TOKEN_KEY ? Promise.resolve('old-refresh') : Promise.resolve(null)
    );
    mockAxiosPost.mockResolvedValue({
      data: { access_token: 'new-access', refresh_token: 'new-refresh' },
    });

    await runResponseErrorInterceptor(make401Error()).catch(() => {});

    expect(mockSetItem).toHaveBeenCalledWith(ACCESS_TOKEN_KEY, 'new-access');
    expect(mockSetItem).toHaveBeenCalledWith(REFRESH_TOKEN_KEY, 'new-refresh');
  });

  it('clears tokens when the server rejects the refresh (401)', async () => {
    mockGetItem.mockImplementation((key: string) =>
      key === REFRESH_TOKEN_KEY ? Promise.resolve('old-refresh') : Promise.resolve(null)
    );
    mockAxiosPost.mockRejectedValue({ response: { status: 401 } });

    await expect(runResponseErrorInterceptor(make401Error())).rejects.toBeDefined();

    expect(mockDeleteItem).toHaveBeenCalledWith(ACCESS_TOKEN_KEY);
    expect(mockDeleteItem).toHaveBeenCalledWith(REFRESH_TOKEN_KEY);
  });
});

// ─── Response interceptor — revoked session (#689) ───────────────────────────

describe('api response interceptor — revoked session', () => {
  const onAuthFailure = jest.fn();

  beforeEach(() => {
    jest.clearAllMocks();
    setAuthFailureCallback(onAuthFailure);
  });

  function makeRevokedError(config: object = {}) {
    return {
      response: { status: 401, data: { detail: TOKEN_REVOKED } },
      config: { headers: {}, ...config },
    };
  }

  it('signs out on 401 token_revoked without trying to refresh', async () => {
    mockGetItem.mockResolvedValue('old-refresh');
    const err = makeRevokedError();

    await expect(runResponseErrorInterceptor(err)).rejects.toBe(err);

    expect(mockAxiosPost).not.toHaveBeenCalled();
    expect(mockDeleteItem).toHaveBeenCalledWith(ACCESS_TOKEN_KEY);
    expect(mockDeleteItem).toHaveBeenCalledWith(REFRESH_TOKEN_KEY);
    expect(onAuthFailure).toHaveBeenCalledTimes(1);
  });

  it('signs out when a retried request is also token_revoked (no loop)', async () => {
    const err = makeRevokedError({ _retry: true });

    await expect(runResponseErrorInterceptor(err)).rejects.toBe(err);

    expect(mockAxiosPost).not.toHaveBeenCalled();
    expect(onAuthFailure).toHaveBeenCalledTimes(1);
  });

  it('signs out when the refresh is rejected after a revocation', async () => {
    mockGetItem.mockImplementation((key: string) =>
      key === REFRESH_TOKEN_KEY ? Promise.resolve('revoked-refresh') : Promise.resolve(null)
    );
    mockAxiosPost.mockRejectedValue({
      response: { status: 401, data: { detail: TOKEN_REVOKED } },
    });

    await expect(runResponseErrorInterceptor(make401Error())).rejects.toBeDefined();

    expect(mockAxiosPost).toHaveBeenCalledTimes(1);
    expect(mockDeleteItem).toHaveBeenCalledWith(ACCESS_TOKEN_KEY);
    expect(mockDeleteItem).toHaveBeenCalledWith(REFRESH_TOKEN_KEY);
    expect(onAuthFailure).toHaveBeenCalledTimes(1);
  });

  it('still refreshes on a plain 401 (expired access token)', async () => {
    mockGetItem.mockImplementation((key: string) =>
      key === REFRESH_TOKEN_KEY ? Promise.resolve('old-refresh') : Promise.resolve(null)
    );
    mockAxiosPost.mockResolvedValue({
      data: { access_token: 'new-access', refresh_token: 'new-refresh' },
    });

    await runResponseErrorInterceptor({
      response: { status: 401, data: { detail: 'Token expired' } },
      config: { headers: {} },
    }).catch(() => {});

    expect(mockAxiosPost).toHaveBeenCalledTimes(1);
    expect(onAuthFailure).not.toHaveBeenCalled();
  });

  // #725: an access token issued before #689 (no tv claim) gets 401
  // token_outdated. The app must refresh and retry, not sign out, so the
  // release that ships #689 does not sign everyone out.
  it('refreshes and retries on 401 token_outdated (legacy access token)', async () => {
    mockGetItem.mockImplementation((key: string) =>
      key === REFRESH_TOKEN_KEY ? Promise.resolve('legacy-refresh') : Promise.resolve(null)
    );
    mockAxiosPost.mockResolvedValue({
      data: { access_token: 'new-access', refresh_token: 'new-refresh' },
    });
    const config: { headers: Record<string, string> } = { headers: {} };

    await runResponseErrorInterceptor({
      response: { status: 401, data: { detail: 'token_outdated' } },
      config,
    }).catch(() => {});

    expect(mockAxiosPost).toHaveBeenCalledTimes(1);
    expect(mockAxiosPost.mock.calls[0][1]).toEqual({ refresh_token: 'legacy-refresh' });
    expect(mockSetItem).toHaveBeenCalledWith(ACCESS_TOKEN_KEY, 'new-access');
    expect(mockSetItem).toHaveBeenCalledWith(REFRESH_TOKEN_KEY, 'new-refresh');
    // Retried with the new access token.
    expect(config.headers.Authorization).toBe('Bearer new-access');
    expect(mockDeleteItem).not.toHaveBeenCalled();
    expect(onAuthFailure).not.toHaveBeenCalled();
  });
});

// ─── Response interceptor — shared refresh (#689) ────────────────────────────

describe('api response interceptor — concurrent 401s share one refresh', () => {
  const onAuthFailure = jest.fn();

  beforeEach(() => {
    jest.clearAllMocks();
    setAuthFailureCallback(onAuthFailure);
    const store: Record<string, string | null> = {
      [ACCESS_TOKEN_KEY]: 'old-access',
      [REFRESH_TOKEN_KEY]: 'old-refresh',
    };
    mockGetItem.mockImplementation((key: string) => Promise.resolve(store[key] ?? null));
    mockSetItem.mockImplementation((key: string, value: string) => {
      store[key] = value;
      return Promise.resolve();
    });
    mockDeleteItem.mockImplementation((key: string) => {
      store[key] = null;
      return Promise.resolve();
    });
  });

  // A per-request adapter answers the retried request without any network,
  // recording the Authorization header it was sent with.
  function make401WithAdapter(name: string) {
    const adapter = jest.fn((config: any) =>
      Promise.resolve({
        data: { name, auth: config.headers.Authorization },
        status: 200,
        statusText: 'OK',
        headers: {},
        config,
      })
    );
    return { adapter, error: make401Error({ url: `/${name}`, adapter }) };
  }

  function deferred<T>() {
    let resolve!: (value: T) => void;
    let reject!: (reason: unknown) => void;
    const promise = new Promise<T>((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  }

  it('posts one refresh for concurrent 401s and retries each with the new token', async () => {
    const refresh = deferred<object>();
    mockAxiosPost.mockReturnValue(refresh.promise);
    const requests = ['a', 'b', 'c'].map(make401WithAdapter);

    const pending = requests.map((r) => runResponseErrorInterceptor(r.error));
    // Let every interceptor reach the shared refresh before it settles.
    await new Promise((r) => setTimeout(r, 0));
    refresh.resolve({ data: { access_token: 'new-access', refresh_token: 'new-refresh' } });
    const responses = await Promise.all(pending);

    expect(mockAxiosPost).toHaveBeenCalledTimes(1);
    expect(mockAxiosPost).toHaveBeenCalledWith(expect.stringContaining('/auth/refresh'), {
      refresh_token: 'old-refresh',
    });
    expect(responses.map((r: any) => r.data)).toEqual([
      { name: 'a', auth: 'Bearer new-access' },
      { name: 'b', auth: 'Bearer new-access' },
      { name: 'c', auth: 'Bearer new-access' },
    ]);
    requests.forEach((r) => expect(r.adapter).toHaveBeenCalledTimes(1));
    expect(onAuthFailure).not.toHaveBeenCalled();
    expect(mockDeleteItem).not.toHaveBeenCalled();
    expect(mockSetItem).toHaveBeenCalledWith(REFRESH_TOKEN_KEY, 'new-refresh');
  });

  it('signs out once when the shared refresh is rejected with 401', async () => {
    const refresh = deferred<object>();
    mockAxiosPost.mockReturnValue(refresh.promise);
    const requests = ['a', 'b'].map(make401WithAdapter);

    const pending = requests.map((r) =>
      runResponseErrorInterceptor(r.error).then(
        () => 'resolved',
        (e) => e
      )
    );
    await new Promise((r) => setTimeout(r, 0));
    refresh.reject({ response: { status: 401, data: { detail: 'Refresh token expired' } } });
    const outcomes = await Promise.all(pending);

    // Each caller gets its own original 401; nothing is retried.
    expect(outcomes).toEqual(requests.map((r) => r.error));
    requests.forEach((r) => expect(r.adapter).not.toHaveBeenCalled());
    expect(mockAxiosPost).toHaveBeenCalledTimes(1);
    expect(onAuthFailure).toHaveBeenCalledTimes(1);
    expect(mockDeleteItem).toHaveBeenCalledWith(ACCESS_TOKEN_KEY);
    expect(mockDeleteItem).toHaveBeenCalledWith(REFRESH_TOKEN_KEY);
  });

  it.each([
    ['429 rate limit', { response: { status: 429 } }],
    ['503 server error', { response: { status: 503 } }],
    ['network error', { request: {}, message: 'Network Error' }],
  ])('keeps the session when the refresh fails with a %s', async (_label, refreshError) => {
    mockAxiosPost.mockRejectedValue(refreshError);
    const { adapter, error } = make401WithAdapter('a');

    await expect(runResponseErrorInterceptor(error)).rejects.toBe(error);

    expect(adapter).not.toHaveBeenCalled();
    expect(onAuthFailure).not.toHaveBeenCalled();
    expect(mockDeleteItem).not.toHaveBeenCalled();
    expect(mockSetItem).not.toHaveBeenCalled();
  });

  it('starts a new refresh once the previous one has settled', async () => {
    mockAxiosPost.mockRejectedValueOnce({ response: { status: 429 } }).mockResolvedValueOnce({
      data: { access_token: 'new-access', refresh_token: 'new-refresh' },
    });

    const first = make401WithAdapter('a');
    await expect(runResponseErrorInterceptor(first.error)).rejects.toBe(first.error);
    const second = make401WithAdapter('b');
    const response: any = await runResponseErrorInterceptor(second.error);

    expect(mockAxiosPost).toHaveBeenCalledTimes(2);
    expect(response.data).toEqual({ name: 'b', auth: 'Bearer new-access' });
    expect(onAuthFailure).not.toHaveBeenCalled();
  });

  it('a token_revoked 401 still signs out without a refresh', async () => {
    const err = {
      response: { status: 401, data: { detail: TOKEN_REVOKED } },
      config: { headers: {} },
    };
    await expect(runResponseErrorInterceptor(err)).rejects.toBe(err);
    expect(mockAxiosPost).not.toHaveBeenCalled();
    expect(onAuthFailure).toHaveBeenCalledTimes(1);
  });

  // A 401 that arrives after the shared refresh has settled, for a request
  // sent with the access token the refresh replaced (#689 review).
  it('retries a stale 401 with the stored token instead of refreshing again', async () => {
    mockAxiosPost.mockResolvedValue({
      data: { access_token: 'new-access', refresh_token: 'new-refresh' },
    });
    const first = make401WithAdapter('a');
    first.error.config.headers = { Authorization: 'Bearer old-access' };
    const late = make401WithAdapter('b');
    late.error.config.headers = { Authorization: 'Bearer old-access' };

    const firstResponse: any = await runResponseErrorInterceptor(first.error);
    const lateResponse: any = await runResponseErrorInterceptor(late.error);

    expect(mockAxiosPost).toHaveBeenCalledTimes(1);
    expect(firstResponse.data).toEqual({ name: 'a', auth: 'Bearer new-access' });
    expect(lateResponse.data).toEqual({ name: 'b', auth: 'Bearer new-access' });
    expect(late.adapter).toHaveBeenCalledTimes(1);
    expect(onAuthFailure).not.toHaveBeenCalled();
  });

  it('refreshes when the 401 was for the stored access token', async () => {
    mockAxiosPost.mockResolvedValue({
      data: { access_token: 'new-access', refresh_token: 'new-refresh' },
    });
    const { error } = make401WithAdapter('a');
    error.config.headers = { Authorization: 'Bearer old-access' };

    const response: any = await runResponseErrorInterceptor(error);

    expect(mockAxiosPost).toHaveBeenCalledTimes(1);
    expect(response.data).toEqual({ name: 'a', auth: 'Bearer new-access' });
  });

  it('a token_revoked 401 for a replaced access token does not sign out', async () => {
    mockAxiosPost.mockResolvedValue({
      data: { access_token: 'new-access', refresh_token: 'new-refresh' },
    });
    // A refresh has already stored a new access token.
    await runResponseErrorInterceptor(make401WithAdapter('a').error);
    expect(mockSetItem).toHaveBeenCalledWith(ACCESS_TOKEN_KEY, 'new-access');

    const err = {
      response: { status: 401, data: { detail: TOKEN_REVOKED } },
      config: { headers: { Authorization: 'Bearer old-access' } },
    };
    await expect(runResponseErrorInterceptor(err)).rejects.toBe(err);

    expect(mockAxiosPost).toHaveBeenCalledTimes(1);
    expect(mockDeleteItem).not.toHaveBeenCalled();
    expect(onAuthFailure).not.toHaveBeenCalled();
  });

  it('a token_revoked 401 for the stored access token signs out', async () => {
    const err = {
      response: { status: 401, data: { detail: TOKEN_REVOKED } },
      config: { headers: { Authorization: 'Bearer old-access' } },
    };
    await expect(runResponseErrorInterceptor(err)).rejects.toBe(err);

    expect(mockAxiosPost).not.toHaveBeenCalled();
    expect(mockDeleteItem).toHaveBeenCalledWith(ACCESS_TOKEN_KEY);
    expect(mockDeleteItem).toHaveBeenCalledWith(REFRESH_TOKEN_KEY);
    expect(onAuthFailure).toHaveBeenCalledTimes(1);
  });
});
