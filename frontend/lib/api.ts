import axios from 'axios';
import type { AxiosRequestConfig } from 'axios';

import { MAX_RETRY_AFTER_MS, isTimeoutError, toApiError } from './apiError';
import { DEFAULT_TIMEOUT_MS } from './apiTimeouts';
import { reportApiError } from './apiErrorReporting';
import { Sentry } from './sentry';
import * as storage from './storage';

const BASE_URL = process.env.EXPO_PUBLIC_API_URL ?? 'http://localhost:8001';

export { DEFAULT_TIMEOUT_MS, SCAN_UPLOAD_TIMEOUT_MS, SEARCH_TIMEOUT_MS } from './apiTimeouts';

export const ACCESS_TOKEN_KEY = 'bookshelf_access_token';
export const REFRESH_TOKEN_KEY = 'bookshelf_refresh_token';

let onAuthFailure: (() => void) | null = null;

// AuthProvider registers its logout function here on mount to avoid circular imports.
export function setAuthFailureCallback(cb: () => void) {
  onAuthFailure = cb;
}

// Session gate (#722). Until AuthProvider's install check confirms that the
// stored tokens belong to this install, requests wait for it. If it resolves
// false (a fresh-install wipe failed, or the install state is unknown), the
// stored tokens are never sent and never refreshed. logout closes the gate;
// login/testLogin lift it once they have stored a new session. Every change
// bumps the session epoch, so a refresh that started before it drops its
// result instead of storing tokens for a session that has ended.
let storedSession: Promise<boolean> = Promise.resolve(true);
let sessionEpoch = 0;

export function gateStoredSession(check: Promise<boolean>) {
  sessionEpoch += 1;
  storedSession = check.catch(() => false);
}

export function resumeStoredSession() {
  sessionEpoch += 1;
  storedSession = Promise.resolve(true);
}

// Responses from these never start a refresh: a rejected sign-in or refresh
// is not an expired access token. '/auth/refresh' is listed for safety only:
// refreshAccessToken posts with bare axios, which bypasses these interceptors.
const NO_REFRESH_PATHS = ['/auth/google', '/auth/refresh'];

// The request's path without baseURL/origin, query, hash or trailing slash.
function requestPath(url: string | undefined): string {
  let path = url ?? '';
  if (path.startsWith(BASE_URL)) path = path.slice(BASE_URL.length);
  path = path.replace(/^[a-z][a-z0-9+.-]*:\/\/[^/]*/i, '');
  path = path.replace(/[?#].*$/, '').replace(/\/+$/, '');
  return path.startsWith('/') ? path : `/${path}`;
}

// Retry policy (#766): at most MAX_RETRIES retries after the first attempt (3
// attempts in all), waiting RETRY_BASE_DELAY_MS * 2^n (1 s, 2 s) plus up to
// 25% jitter, or the server's Retry-After (capped at MAX_RETRY_AFTER_MS).
export const MAX_RETRIES = 2;
const RETRY_BASE_DELAY_MS = 1_000;
const RETRY_JITTER_RATIO = 0.25;
/** Statuses worth retrying; a network error with no response is too (a timeout is not). */
const RETRYABLE_STATUSES: readonly number[] = [408, 429, 502, 503, 504];
// TODO(#685): POST and PATCH retry only once the server honours an
// Idempotency-Key; until then a retried write could be applied twice.
const IDEMPOTENT_METHODS: readonly string[] = ['get', 'head', 'put', 'delete', 'options'];
// Never retried here: the scan queue (#604) owns the /scan retry schedule.
const NO_RETRY_PATHS = ['/scan'];

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
let retrySleep: (ms: number) => Promise<void> = defaultSleep;
let retryRandom: () => number = Math.random;

/** Test hook: replace the retry sleeper and/or jitter source. No args resets both. */
export function configureRetry(opts?: {
  sleep?: (ms: number) => Promise<void>;
  random?: () => number;
}) {
  retrySleep = opts?.sleep ?? defaultSleep;
  retryRandom = opts?.random ?? Math.random;
}

/** Wait before retry number `attempt` (0-based): Retry-After if sent, else backoff + jitter. */
function retryDelayMs(attempt: number, retryAfterMs?: number): number {
  if (retryAfterMs !== undefined) return Math.min(retryAfterMs, MAX_RETRY_AFTER_MS);
  const base = RETRY_BASE_DELAY_MS * 2 ** attempt;
  return Math.round(base * (1 + retryRandom() * RETRY_JITTER_RATIO));
}

export const api = axios.create({
  baseURL: BASE_URL,
  timeout: DEFAULT_TIMEOUT_MS,
  headers: { 'Content-Type': 'application/json' },
});

type SessionTagged = { _sessionEpoch?: number };
type RetryTagged = { _retryCount?: number };

function shouldRetry(
  error: unknown,
  config: (AxiosRequestConfig & RetryTagged & SessionTagged) | undefined
) {
  if (!config) return false;
  if (axios.isCancel(error) || config.signal?.aborted) return false;
  if (!IDEMPOTENT_METHODS.includes(String(config.method ?? 'get').toLowerCase())) return false;
  if (NO_RETRY_PATHS.includes(requestPath(config.url))) return false;
  if ((config._retryCount ?? 0) >= MAX_RETRIES) return false;
  // A request from an earlier session is never replayed with the new session's token.
  if (config._sessionEpoch !== undefined && config._sessionEpoch !== sessionEpoch) return false;
  // A timeout is never retried: it means the server may still be working (a
  // search runs provider calls for up to ~30 s), not that the request was
  // lost, and a re-send would multiply the load. Other network errors
  // (connection refused/reset, offline) are retried.
  if (isTimeoutError(error)) return false;
  const status = toApiError(error).status;
  return status === undefined || RETRYABLE_STATUSES.includes(status);
}

// Inject access token on every request and add Sentry breadcrumb
api.interceptors.request.use(async (config) => {
  Sentry.addBreadcrumb({
    category: 'http',
    message: `${(config.method ?? 'GET').toUpperCase()} ${config.url}`,
    level: 'info',
  });
  const usable = await storedSession;
  // The session this request is sent in; see the 401 handler.
  (config as SessionTagged)._sessionEpoch = sessionEpoch;
  if (usable) {
    const token = await storage.getItem(ACCESS_TOKEN_KEY);
    if (token) {
      config.headers.Authorization = `Bearer ${token}`;
    }
  }
  return config;
});

// 401 detail when the session was revoked server-side (sign out of all
// devices, #689). Refreshing cannot help, so the app signs out at once.
export const TOKEN_REVOKED = 'token_revoked';

async function signOut() {
  await storage.deleteItem(ACCESS_TOKEN_KEY);
  await storage.deleteItem(REFRESH_TOKEN_KEY);
  onAuthFailure?.();
}

async function dropWrittenTokens(access: string, refresh: string) {
  if ((await storage.getItem(ACCESS_TOKEN_KEY)) === access) {
    await storage.deleteItem(ACCESS_TOKEN_KEY);
  }
  if ((await storage.getItem(REFRESH_TOKEN_KEY)) === refresh) {
    await storage.deleteItem(REFRESH_TOKEN_KEY);
  }
}

// One refresh at a time. The refresh token is single-use (rotated on every
// /auth/refresh), so concurrent 401s must share one refresh: a second POST
// with the same token would be rejected and sign the user out, wiping the
// tokens the first one just stored. Cleared when it settles.
let refreshInFlight: Promise<string | null> | null = null;

// Resolves once any in-flight refresh has settled (errors swallowed), so a
// caller can read the freshly rotated tokens, or know none will be written.
export async function waitForPendingRefresh(): Promise<void> {
  try {
    await refreshInFlight;
  } catch {
    // Ignored: only settling matters.
  }
}

// Resolves to the new access token, or null when there is none to retry
// with. Signs out only when the server rejects the refresh (401, including
// token_revoked and refresh_reused, #690: a replayed refresh token revoked
// every session); a 429, 5xx or network error keeps the stored tokens.
function refreshAccessToken(): Promise<string | null> {
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      const epoch = sessionEpoch;
      const refreshToken = await storage.getItem(REFRESH_TOKEN_KEY);
      if (!refreshToken) return null;
      try {
        const { data } = await axios.post(`${BASE_URL}/auth/refresh`, {
          refresh_token: refreshToken,
        });
        // Signed out (or into a new session) meanwhile: drop the result.
        if (epoch !== sessionEpoch) return null;
        await storage.setItem(ACCESS_TOKEN_KEY, data.access_token);
        await storage.setItem(REFRESH_TOKEN_KEY, data.refresh_token);
        // A sign-out (or new login) began during the writes: remove what this
        // refresh wrote, unless a newer session has replaced it already.
        if (epoch !== sessionEpoch) {
          await dropWrittenTokens(data.access_token, data.refresh_token);
          return null;
        }
        return data.access_token as string;
      } catch (refreshError) {
        const status = (refreshError as { response?: { status?: number } })?.response?.status;
        if (status === 401 && epoch === sessionEpoch) {
          await signOut();
        }
        return null;
      }
    })().finally(() => {
      refreshInFlight = null;
    });
  }
  return refreshInFlight;
}

// The access token a request was sent with, or null if it carried none.
function sentAccessToken(config: { headers?: Record<string, unknown> } | undefined) {
  const auth = config?.headers?.Authorization;
  return typeof auth === 'string' && auth.startsWith('Bearer ') ? auth.slice(7) : null;
}

// On 401: refresh once (shared by every request that got a 401 meanwhile),
// then retry with the new access token. A revoked session (401
// token_revoked) signs the user out at once, without a refresh.
//
// A 401 for a request sent with an access token that is no longer the
// stored one is stale: the session has moved on (a refresh has already
// stored a new token, or the user signed out). Such a 401 neither signs out
// nor starts another refresh; a plain 401 is retried once with the stored
// token instead.
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config;
    if (error.response?.status !== 401) {
      // #766: transient failures of idempotent calls are retried here. Only
      // the final failure is reported below (the replayed request passes
      // through this handler again), so Sentry sees one event per request.
      if (shouldRetry(error, original)) {
        const attempt = original._retryCount ?? 0;
        original._retryCount = attempt + 1;
        await retrySleep(retryDelayMs(attempt, toApiError(error).retryAfterMs));
        // Signed out, switched account or aborted while waiting: the request
        // belongs to a session that is gone, so never replay it (the request
        // interceptor would send it with the new session's token). Like the
        // stale-401 path below, it is rejected unreported.
        const sentEpoch = (original as SessionTagged)._sessionEpoch;
        if (original.signal?.aborted || (sentEpoch !== undefined && sentEpoch !== sessionEpoch)) {
          return Promise.reject(error);
        }
        return api(original);
      }
      // #724: a 403 (incl. `forbidden`) is not an auth failure; it never
      // signs the user out. Report unexpected failures once per session.
      reportApiError(error);
      return Promise.reject(error);
    }
    if (NO_REFRESH_PATHS.includes(requestPath(original?.url)) || !(await storedSession)) {
      return Promise.reject(error);
    }
    // Sent in an earlier session (a sign-out, and maybe a new sign-in, since):
    // never retry it, or it would be replayed with the new session's token.
    const sentEpoch = (original as SessionTagged | undefined)?._sessionEpoch;
    if (sentEpoch !== undefined && sentEpoch !== sessionEpoch) {
      return Promise.reject(error);
    }
    const sent = sentAccessToken(original);
    const stored = await storage.getItem(ACCESS_TOKEN_KEY);
    const stale = sent !== null && sent !== stored;
    if (error.response.data?.detail === TOKEN_REVOKED) {
      if (!stale) {
        await signOut();
      }
      return Promise.reject(error);
    }
    if (!original._retry) {
      original._retry = true;
      const accessToken = stale ? stored : await refreshAccessToken();
      if (accessToken) {
        original.headers.Authorization = `Bearer ${accessToken}`;
        return api(original);
      }
    }
    return Promise.reject(error);
  }
);

/**
 * A library entry in the trash (#683): the `UserBookRead` shape plus when it
 * was deleted and when the daily cleanup may delete it for good.
 */
export interface TrashedUserBook {
  id: string;
  status: string;
  rating: number | null;
  notes: string | null;
  book: {
    id: string;
    title: string;
    author: string;
    cover_url: string | null;
    description: string | null;
  };
  /** A book the user added themselves; the catalog could not find it (#616). */
  unverified?: boolean;
  deleted_at: string;
  purge_after: string;
}

/** GET /user-books/trash: newest deletion first, at most 1,000 entries. */
export async function listTrash(): Promise<TrashedUserBook[]> {
  const { data } = await api.get<TrashedUserBook[]>('/user-books/trash');
  return data;
}

/**
 * POST /user-books/{id}/restore: moves an entry out of the trash. Rejects with
 * the axios error; `restoreFailure` (lib/trash.ts) maps 404 and the two 409
 * codes (`restore_conflict`, `unverified_limit_reached`).
 */
export async function restoreUserBook(id: string): Promise<unknown> {
  const { data } = await api.post<unknown>(`/user-books/${encodeURIComponent(id)}/restore`);
  return data;
}
