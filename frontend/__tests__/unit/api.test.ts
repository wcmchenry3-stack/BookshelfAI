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

  it('clears tokens when refresh request itself fails', async () => {
    mockGetItem.mockImplementation((key: string) =>
      key === REFRESH_TOKEN_KEY ? Promise.resolve('old-refresh') : Promise.resolve(null)
    );
    mockAxiosPost.mockRejectedValue(new Error('refresh failed'));

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
});
