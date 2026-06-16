import React, { createContext, useCallback, useEffect, useState } from 'react';
import { useRouter, useSegments } from 'expo-router';

import { ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY, api, setAuthFailureCallback } from '../lib/api';
import * as storage from '../lib/storage';

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

  // Check for stored token on mount
  useEffect(() => {
    storage.getItem(ACCESS_TOKEN_KEY).then((token) => {
      setIsAuthenticated(!!token);
      setIsLoading(false);
    });
  }, []);

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
