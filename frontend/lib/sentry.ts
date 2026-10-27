import * as Sentry from '@sentry/react-native';
import { filterExpectedNetworkNoise } from './sentryFilter';

const DSN = process.env.EXPO_PUBLIC_SENTRY_DSN;

/**
 * Initialise Sentry (both JS and native layers).
 *
 * @sentry/react-native v7 auto-initialises the native SDK via the RN
 * bridge (autoInitializeNativeSdk defaults to true) — no separate native
 * call in AppDelegate is needed.
 *
 * Wrapped in try/catch so a synchronous init error can never crash the
 * app before anything renders. Async bridge errors are handled by the
 * ErrorBoundary in _layout.tsx.
 */
export function initSentry(): void {
  if (!DSN) {
    console.warn('[Sentry] No DSN configured, skipping init');
    return;
  }

  try {
    Sentry.init({
      dsn: DSN,
      environment: process.env.EXPO_PUBLIC_ENVIRONMENT ?? 'development',
      tracesSampleRate: process.env.EXPO_PUBLIC_ENVIRONMENT === 'production' ? 0.2 : 1.0,
      sendDefaultPii: false,
      enabled: !!DSN,
      // #701: drop only known-benign user-environment network noise (narrow
      // matcher in sentryFilter.ts); leave a breadcrumb so an outage is visible.
      beforeSend: (event, hint) =>
        filterExpectedNetworkNoise(event, hint, () =>
          Sentry.addBreadcrumb({
            category: 'sentry.filter',
            message: 'Dropped expected client network error',
            level: 'info',
          })
        ),
    });

    Sentry.addBreadcrumb({
      category: 'app.lifecycle',
      message: 'Sentry init completed',
      level: 'info',
      data: { environment: process.env.EXPO_PUBLIC_ENVIRONMENT ?? 'development' },
    });
  } catch (e) {
    // Log but never crash — the app must boot even if Sentry is broken.
    console.error('[Sentry] init failed:', e);
  }
}

// Self-initialise on import so Sentry is ready before any other module
// evaluates.  _layout.tsx must import this file FIRST.
initSentry();

export { Sentry };
