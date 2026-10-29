import React, { createContext, useCallback, useMemo, useState } from 'react';
import { Platform } from 'react-native';

import { TurnstileWidget } from '../components/TurnstileWidget';
import { Sentry } from '../lib/sentry';
import { turnstileBaseUrl, turnstileSiteKey } from '../lib/turnstile/config';
import { TurnstileTokenBroker } from '../lib/turnstile/tokenBroker';

export interface TurnstileContextValue {
  /**
   * A fresh Cloudflare Turnstile token for one POST /scan attempt (#740), or
   * null: no site key configured, the widget timed out (about 10 s) or
   * failed, or `signal` was aborted. The scan is then sent without a token.
   * Never rejects. Call it once per attempt: tokens are single-use.
   */
  getToken: (signal?: AbortSignal) => Promise<string | null>;
  /**
   * Whether this build can produce tokens at all (a site key, and on native a
   * base URL). False: every request goes without one, so a server answer of
   * 400 "Missing Turnstile token" cannot be fixed by retrying.
   */
  configured: boolean;
}

export const TurnstileContext = createContext<TurnstileContextValue>({
  getToken: async () => null,
  configured: false,
});

// Logged once per app session, not per scan.
let missingConfigLogged = false;

/** Tests only. */
export function resetTurnstileLoggingForTests(): void {
  missingConfigLogged = false;
}

function logMissingConfigOnce(what: 'site_key' | 'base_url') {
  if (missingConfigLogged) return;
  missingConfigLogged = true;
  Sentry.addBreadcrumb({
    category: 'turnstile',
    message: `Turnstile not configured (${what}): /scan is sent without a token`,
    level: 'info',
  });
}

/**
 * Mounts the one Turnstile widget (a hidden WebView on iOS/Android, an
 * invisible widget on web) and provides `getToken`. No visible UI. Place it
 * above ScanJobProvider.
 */
export function TurnstileProvider({ children }: { children: React.ReactNode }) {
  // One broker (and so one widget) for the provider's lifetime.
  const [broker] = useState(() => new TurnstileTokenBroker());
  const siteKey = turnstileSiteKey();
  const baseUrl = turnstileBaseUrl();
  // The native page needs an origin for Turnstile's hostname check.
  const missing = !siteKey ? 'site_key' : Platform.OS !== 'web' && !baseUrl ? 'base_url' : null;

  const getToken = useCallback(
    async (signal?: AbortSignal) => {
      if (missing) {
        logMissingConfigOnce(missing);
        return null;
      }
      return broker.request(signal);
    },
    [broker, missing]
  );

  const configured = missing === null;
  const value = useMemo(() => ({ getToken, configured }), [getToken, configured]);

  return (
    <TurnstileContext.Provider value={value}>
      {children}
      {siteKey && !missing ? (
        <TurnstileWidget broker={broker} siteKey={siteKey} baseUrl={baseUrl} />
      ) : null}
    </TurnstileContext.Provider>
  );
}
