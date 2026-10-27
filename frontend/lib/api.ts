import axios from 'axios';

import { Sentry } from './sentry';
import * as storage from './storage';

const BASE_URL = process.env.EXPO_PUBLIC_API_URL ?? 'http://localhost:8001';

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
// is not an expired access token.
const NO_REFRESH_PATHS = ['/auth/google', '/auth/refresh'];

export const api = axios.create({
  baseURL: BASE_URL,
  headers: { 'Content-Type': 'application/json' },
});

// Inject access token on every request and add Sentry breadcrumb
api.interceptors.request.use(async (config) => {
  Sentry.addBreadcrumb({
    category: 'http',
    message: `${(config.method ?? 'GET').toUpperCase()} ${config.url}`,
    level: 'info',
  });
  if (await storedSession) {
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

// One refresh at a time. The refresh token is single-use (rotated on every
// /auth/refresh), so concurrent 401s must share one refresh: a second POST
// with the same token would be rejected and sign the user out, wiping the
// tokens the first one just stored. Cleared when it settles.
let refreshInFlight: Promise<string | null> | null = null;

// Resolves to the new access token, or null when there is none to retry
// with. Signs out only when the server rejects the refresh (401, including
// token_revoked); a 429, 5xx or network error keeps the stored tokens.
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
      return Promise.reject(error);
    }
    if (NO_REFRESH_PATHS.includes(original?.url) || !(await storedSession)) {
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
