import React, { createContext, useCallback, useEffect, useState } from 'react';
import { useRouter, useSegments } from 'expo-router';

import { ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY, api, setAuthFailureCallback } from '../lib/api';
import { clearTokensIfFreshInstall } from '../lib/freshInstall';
import * as storage from '../lib/storage';

// How long launch waits for GET /auth/me before carrying on signed in (#722).
export const SESSION_CHECK_TIMEOUT_MS = 8000;

type SessionCheck = 'valid' | 'dead' | 'unknown';

// Confirms a stored session with GET /auth/me. The api interceptor refreshes
// on 401 and signs out (deleting both tokens) when the refresh is rejected.
// The session is 'dead' only when that happened, or when a 401 cannot be
// refreshed because there is no refresh token. A network error, timeout,
// 5xx or 403 is 'unknown', and the app stays signed in so offline use works.
async function checkSession(): Promise<SessionCheck> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const timeout = new Promise<SessionCheck>((resolve) => {
    timer = setTimeout(() => resolve('unknown'), SESSION_CHECK_TIMEOUT_MS);
  });
  const request = (async (): Promise<SessionCheck> => {
    try {
      await api.get('/auth/me', { timeout: SESSION_CHECK_TIMEOUT_MS });
      return 'valid';
    } catch (error) {
      if ((error as { response?: { status?: number } })?.response?.status !== 401) {
        return 'unknown';
      }
      const [access, refresh] = await Promise.all([
        storage.getItem(ACCESS_TOKEN_KEY),
        storage.getItem(REFRESH_TOKEN_KEY),
      ]);
      return access && refresh ? 'unknown' : 'dead';
    }
  })();
  try {
    return await Promise.race([request, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

interface AuthContextValue {
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (idToken: string) => Promise<void>;
  testLogin: (accessToken: string, refreshToken: string) => Promise<void>;
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue>({
  isAuthenticated: false,
  isLoading: true,
  login: async () => {},
  testLogin: async () => {},
  logout: async () => {},
});

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [isAuthenticated, setIsAuthenticated] = useState(false);
  const [isLoading, setIsLoading] = useState(true);
  const segments = useSegments();
  const router = useRouter();
  // Started while rendering, before any child effect touches AsyncStorage
  // (see lib/freshInstall.ts). Memoised, so a repeat render reuses it.
  const [installCheck] = useState(clearTokensIfFreshInstall);

  // On mount: clear tokens left in the Keychain by a previous install, then
  // confirm any stored session with the server before leaving the loading
  // state (#722).
  useEffect(() => {
    let cancelled = false;
    (async () => {
      await installCheck;
      const token = await storage.getItem(ACCESS_TOKEN_KEY);
      if (!token) {
        if (!cancelled) {
          setIsAuthenticated(false);
          setIsLoading(false);
        }
        return;
      }
      const session = await checkSession();
      if (session === 'dead') {
        await storage.deleteItem(ACCESS_TOKEN_KEY);
        await storage.deleteItem(REFRESH_TOKEN_KEY);
      }
      if (!cancelled) {
        setIsAuthenticated(session !== 'dead');
        setIsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [installCheck]);

  const login = useCallback(async (idToken: string) => {
    const { data } = await api.post('/auth/google', { id_token: idToken });
    await storage.setItem(ACCESS_TOKEN_KEY, data.access_token);
    await storage.setItem(REFRESH_TOKEN_KEY, data.refresh_token);
    setIsAuthenticated(true);
  }, []);

  const testLogin = useCallback(async (accessToken: string, refreshToken: string) => {
    await storage.setItem(ACCESS_TOKEN_KEY, accessToken);
    await storage.setItem(REFRESH_TOKEN_KEY, refreshToken);
    setIsAuthenticated(true);
  }, []);

  const logout = useCallback(async () => {
    await storage.deleteItem(ACCESS_TOKEN_KEY);
    await storage.deleteItem(REFRESH_TOKEN_KEY);
    setIsAuthenticated(false);
  }, []);

  // Register logout as the auth-failure callback so api.ts can signal session expiry
  useEffect(() => {
    setAuthFailureCallback(logout);
  }, [logout]);

  // Redirect based on auth state
  useEffect(() => {
    if (isLoading) return;
    const inAuthGroup = segments[0] === '(auth)';
    if (!isAuthenticated && !inAuthGroup) {
      router.replace('/(auth)/login');
    } else if (isAuthenticated && inAuthGroup) {
      router.replace('/(tabs)/my-books');
    }
  }, [isAuthenticated, isLoading, segments, router]);

  return (
    <AuthContext.Provider value={{ isAuthenticated, isLoading, login, testLogin, logout }}>
      {children}
    </AuthContext.Provider>
  );
}
