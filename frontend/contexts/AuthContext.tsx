import React, { createContext, useCallback, useEffect, useRef, useState } from 'react';
import { useRouter, useSegments } from 'expo-router';

import {
  ACCESS_TOKEN_KEY,
  REFRESH_TOKEN_KEY,
  api,
  gateStoredSession,
  resumeStoredSession,
  setAuthFailureCallback,
} from '../lib/api';
import {
  type InstallCheckResult,
  clearTokensIfFreshInstall,
  markInstallDone,
  markWipePending,
  storedTokensUsable,
  supersedeInstallCheck,
} from '../lib/freshInstall';
import { Sentry } from '../lib/sentry';
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
      // Both tokens still there after a 401 means the refresh failed for a
      // passing reason (network, 5xx, 429), so the session is kept. It also
      // covers a refresh that succeeded but whose retry still got a 401; that
      // needs an inconsistent server, so it is not handled here (#729 review).
      return access && refresh ? 'unknown' : 'dead';
    }
  })();
  try {
    return await Promise.race([request, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

// How long requests and the loading state wait for the install check. Past
// it the launch is treated as signed out (fail closed); login reopens the
// gate, so the user can still sign in.
export const INSTALL_CHECK_TIMEOUT_MS = 5000;

// Whether the stored tokens may be used, or false if the check takes longer
// than INSTALL_CHECK_TIMEOUT_MS.
function usableWithin(check: Promise<InstallCheckResult>): Promise<boolean> {
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      Sentry.addBreadcrumb({
        category: 'auth',
        message: 'install check timed out',
        level: 'warning',
      });
      resolve(false);
    }, INSTALL_CHECK_TIMEOUT_MS);
    check
      .then(storedTokensUsable, () => false)
      .then((usable) => {
        clearTimeout(timer);
        resolve(usable);
      });
  });
}

// The install check the request layer was last gated on. The check is
// memoised per launch, so a remount (or a StrictMode double render) must not
// gate again on the same check after login has lifted the gate.
let gated: { check: Promise<InstallCheckResult>; usable: Promise<boolean> } | null = null;

// Starts the install check (lib/freshInstall.ts) and gates the request layer
// on it, so no request sends or refreshes the stored tokens until the check
// says they belong to this install. Resolves to whether they may be used.
function startInstallCheck(): Promise<boolean> {
  const check = clearTokensIfFreshInstall();
  if (gated?.check !== check) {
    gated = { check, usable: usableWithin(check) };
    gateStoredSession(gated.usable);
  }
  return gated.usable;
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
  // Started while rendering, before any child effect touches AsyncStorage or
  // makes a request (see lib/freshInstall.ts). Memoised, so a repeat render
  // reuses it.
  const [tokensUsable] = useState(startInstallCheck);
  // Set by login, testLogin and logout. The launch check below may still be
  // running (the install check alone can take 5 s); once the user has signed
  // in or out, its result must not overwrite theirs.
  const sessionChangedRef = useRef(false);

  // On mount: clear tokens left in the Keychain by a previous install, then
  // confirm any stored session with the server before leaving the loading
  // state (#722). Whatever throws, loading always ends: signed in only if a
  // token was read and the session was not found dead, else signed out.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      let authed = false;
      try {
        // A failed wipe may have left the previous install's tokens, and a
        // failed check cannot tell: this launch is signed out (the request
        // layer is gated too, so the tokens are neither sent nor refreshed).
        if (!(await tokensUsable)) return;
        const token = await storage.getItem(ACCESS_TOKEN_KEY);
        if (!token) return;
        authed = true;
        const session = await checkSession();
        if (session === 'dead') {
          authed = false;
          await storage.deleteItem(ACCESS_TOKEN_KEY);
          await storage.deleteItem(REFRESH_TOKEN_KEY);
        }
      } catch {
        Sentry.addBreadcrumb({
          category: 'auth',
          message: 'launch session check failed',
          level: 'warning',
        });
      } finally {
        if (!cancelled) {
          if (!sessionChangedRef.current) setIsAuthenticated(authed);
          setIsLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tokensUsable]);

  // Stores a new session. The install check is superseded first, so a check
  // still running (past its 5 s cap) cannot delete the new tokens; once both
  // are written the install is marked done, so a 'pending' marker left by a
  // failed check, wipe or sign-out does not wipe them on the next launch.
  const storeSession = useCallback(async (accessToken: string, refreshToken: string) => {
    await supersedeInstallCheck();
    await storage.setItem(ACCESS_TOKEN_KEY, accessToken);
    await storage.setItem(REFRESH_TOKEN_KEY, refreshToken);
    await markInstallDone();
    sessionChangedRef.current = true;
    resumeStoredSession();
    setIsAuthenticated(true);
  }, []);

  const login = useCallback(
    async (idToken: string) => {
      const { data } = await api.post('/auth/google', { id_token: idToken });
      await storeSession(data.access_token, data.refresh_token);
    },
    [storeSession]
  );

  const testLogin = useCallback(
    (accessToken: string, refreshToken: string) => storeSession(accessToken, refreshToken),
    [storeSession]
  );

  // Signs out even if SecureStore throws: the request gate is closed first
  // (so a refresh landing during the deletes is dropped), both deletes are
  // attempted, and the state always becomes signed out. If a delete fails,
  // the install marker is set to 'pending' so the next launch wipes the
  // tokens before using them.
  const logout = useCallback(async () => {
    gateStoredSession(Promise.resolve(false));
    try {
      const results = await Promise.allSettled([
        storage.deleteItem(ACCESS_TOKEN_KEY),
        storage.deleteItem(REFRESH_TOKEN_KEY),
      ]);
      if (results.some((r) => r.status === 'rejected')) {
        await markWipePending();
      }
    } finally {
      // Close the request gate even if a delete failed, so any token left
      // behind is not sent, and a refresh already in flight is dropped.
      gateStoredSession(Promise.resolve(false));
      sessionChangedRef.current = true;
      setIsAuthenticated(false);
    }
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
