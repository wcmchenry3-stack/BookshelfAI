import React, { createContext, useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { AiConsentPrompt } from '../components/AiConsentPrompt';
import { useAuth } from '../hooks/useAuth';
import {
  grantAiConsent,
  loadAiConsent,
  withdrawAiConsent,
  type AiConsentState,
} from '../lib/aiConsent';
import { Sentry } from '../lib/sentry';

/** `unknown` until the server has answered for this account (or after sign-out). */
export type AiConsentStatus = 'unknown' | 'given' | 'none';

export interface AiConsentContextValue {
  status: AiConsentStatus;
  /** When consent was last given (ISO, server time), while it stands. */
  givenAt: string | null;
  /**
   * Interactive: resolves true once consent stands on the server, asking with
   * the prompt when it does not (Continue records it; Not now resolves
   * false). With `allowUnknown`, a state that cannot be read (offline)
   * resolves true without asking: the caller only keeps the photo on the
   * device, and the upload checks again (`checkConsent`).
   */
  ensureConsent: (options?: { allowUnknown?: boolean }) => Promise<boolean>;
  /** Non-interactive: true only when consent is known to stand (reads it when unknown). */
  checkConsent: () => Promise<boolean>;
  /** The server refused a scan for lack of consent: forget it here too. */
  markRequired: () => void;
  /** Settings: withdraw consent. Resolves true once the server stored it. */
  withdraw: () => Promise<boolean>;
}

export const AiConsentContext = createContext<AiConsentContextValue>({
  status: 'unknown',
  givenAt: null,
  ensureConsent: async () => false,
  checkConsent: async () => false,
  markRequired: () => {},
  withdraw: async () => false,
});

/**
 * Consent to send scan photos to the AI provider (#1023). Holds this
 * account's state (read lazily from `GET /auth/me` when first needed, never
 * at launch), shows the prompt when a scan asks for it, and records the
 * answer on the server. Nothing is stored on the device: a reinstall or a
 * second device reads the same state.
 */
export function AiConsentProvider({ children }: { children: React.ReactNode }) {
  const { isAuthenticated } = useAuth();
  const [status, setStatusState] = useState<AiConsentStatus>('unknown');
  const [givenAt, setGivenAt] = useState<string | null>(null);
  const statusRef = useRef<AiConsentStatus>('unknown');
  // Bumped on sign-out: an answer for the previous account is dropped.
  const generationRef = useRef(0);
  const loadingRef = useRef<Promise<AiConsentStatus> | null>(null);
  // Bumped whenever this app changes the state (grant, withdraw, a server
  // refusal): a read that was in flight meanwhile is older, so it is dropped.
  const versionRef = useRef(0);
  // The open prompt's answer; every caller while it is open shares it.
  const pendingRef = useRef<{ promise: Promise<boolean>; resolve: (ok: boolean) => void } | null>(
    null
  );
  const [promptVisible, setPromptVisible] = useState(false);
  const [saving, setSaving] = useState(false);
  const [failed, setFailed] = useState(false);
  const savingRef = useRef(false);

  const apply = useCallback((state: AiConsentState) => {
    const next: AiConsentStatus = state.given ? 'given' : 'none';
    statusRef.current = next;
    setStatusState(next);
    setGivenAt(state.given ? state.givenAt : null);
    return next;
  }, []);

  const closePrompt = useCallback((ok: boolean) => {
    const pending = pendingRef.current;
    pendingRef.current = null;
    setPromptVisible(false);
    setFailed(false);
    pending?.resolve(ok);
  }, []);

  // Sign-out (or signed out at launch): forget the previous account's answer.
  const wasAuthenticatedRef = useRef(isAuthenticated);
  useEffect(() => {
    const was = wasAuthenticatedRef.current;
    wasAuthenticatedRef.current = isAuthenticated;
    if (!was || isAuthenticated) return;
    generationRef.current += 1;
    loadingRef.current = null;
    statusRef.current = 'unknown';
    setStatusState('unknown');
    setGivenAt(null);
    closePrompt(false);
  }, [isAuthenticated, closePrompt]);

  /** Reads the state once at a time; failures leave it unknown (tried again next time). */
  const load = useCallback((): Promise<AiConsentStatus> => {
    if (loadingRef.current) return loadingRef.current;
    const generation = generationRef.current;
    const version = versionRef.current;
    const loading = (async () => {
      const state = await loadAiConsent();
      if (generationRef.current !== generation) return 'unknown' as const;
      if (!state || versionRef.current !== version) return statusRef.current;
      return apply(state);
    })();
    loadingRef.current = loading;
    void loading.finally(() => {
      if (loadingRef.current === loading) loadingRef.current = null;
    });
    return loading;
  }, [apply]);

  const checkConsent = useCallback(async () => {
    if (statusRef.current === 'given') return true;
    if (statusRef.current === 'unknown') return (await load()) === 'given';
    return false;
  }, [load]);

  const ensureConsent = useCallback(
    async ({ allowUnknown = false }: { allowUnknown?: boolean } = {}) => {
      const generation = generationRef.current;
      if (statusRef.current === 'unknown') await load();
      // Signed out while the state was read: never ask for the next account.
      if (generationRef.current !== generation) return false;
      if (statusRef.current === 'given') return true;
      if (statusRef.current === 'unknown' && allowUnknown) return true;
      if (pendingRef.current) return pendingRef.current.promise;
      let resolve!: (ok: boolean) => void;
      const promise = new Promise<boolean>((r) => {
        resolve = r;
      });
      pendingRef.current = { promise, resolve };
      setFailed(false);
      setPromptVisible(true);
      Sentry.addBreadcrumb({ category: 'ai-consent', message: 'prompt shown', level: 'info' });
      return promise;
    },
    [load]
  );

  const onContinue = useCallback(async () => {
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setFailed(false);
    const generation = generationRef.current;
    try {
      const state = await grantAiConsent();
      if (generationRef.current !== generation) return;
      versionRef.current += 1;
      if (apply(state) === 'given') {
        Sentry.addBreadcrumb({ category: 'ai-consent', message: 'given', level: 'info' });
        closePrompt(true);
      } else {
        setFailed(true);
      }
    } catch {
      // The prompt stays open with the error, so Continue can be tried again.
      if (generationRef.current === generation) setFailed(true);
      Sentry.addBreadcrumb({ category: 'ai-consent', message: 'save failed', level: 'warning' });
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }, [apply, closePrompt]);

  const onNotNow = useCallback(() => {
    if (savingRef.current) return;
    Sentry.addBreadcrumb({ category: 'ai-consent', message: 'not now', level: 'info' });
    closePrompt(false);
  }, [closePrompt]);

  const markRequired = useCallback(() => {
    versionRef.current += 1;
    statusRef.current = 'none';
    setStatusState('none');
    setGivenAt(null);
  }, []);

  const withdraw = useCallback(async () => {
    const generation = generationRef.current;
    try {
      const state = await withdrawAiConsent();
      if (generationRef.current === generation) {
        versionRef.current += 1;
        apply(state);
      }
      return !state.given;
    } catch {
      Sentry.addBreadcrumb({
        category: 'ai-consent',
        message: 'withdraw failed',
        level: 'warning',
      });
      return false;
    }
  }, [apply]);

  const value = useMemo(
    () => ({ status, givenAt, ensureConsent, checkConsent, markRequired, withdraw }),
    [status, givenAt, ensureConsent, checkConsent, markRequired, withdraw]
  );

  return (
    <AiConsentContext.Provider value={value}>
      {children}
      <AiConsentPrompt
        visible={promptVisible}
        saving={saving}
        failed={failed}
        onContinue={() => void onContinue()}
        onNotNow={onNotNow}
      />
    </AiConsentContext.Provider>
  );
}
