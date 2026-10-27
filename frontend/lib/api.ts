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
  const token = await storage.getItem(ACCESS_TOKEN_KEY);
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
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

// On 401: attempt token refresh once, then give up. A revoked session
// (401 token_revoked) or a failed refresh signs the user out.
api.interceptors.response.use(
  (response) => response,
  async (error) => {
    const original = error.config;
    if (error.response?.status === 401 && error.response.data?.detail === TOKEN_REVOKED) {
      await signOut();
      return Promise.reject(error);
    }
    if (error.response?.status === 401 && !original._retry) {
      original._retry = true;
      const refreshToken = await storage.getItem(REFRESH_TOKEN_KEY);
      if (refreshToken) {
        try {
          const { data } = await axios.post(`${BASE_URL}/auth/refresh`, {
            refresh_token: refreshToken,
          });
          await storage.setItem(ACCESS_TOKEN_KEY, data.access_token);
          await storage.setItem(REFRESH_TOKEN_KEY, data.refresh_token);
          original.headers.Authorization = `Bearer ${data.access_token}`;
          return api(original);
        } catch {
          await signOut();
        }
      }
    }
    return Promise.reject(error);
  }
);
