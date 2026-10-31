import * as Sentry from '@sentry/react-native';
import type { EventHint } from '@sentry/core';
import { filterExpectedNetworkNoise } from './sentryFilter';
import { scrubBreadcrumb, scrubSentryEvent } from './sentryScrub';

const DSN = process.env.EXPO_PUBLIC_SENTRY_DSN;

/**
 * #701: drop only known-benign user-environment network noise (narrow matcher
 * in sentryFilter.ts); leave a breadcrumb so an outage is visible.
 * #736: then replace an unhandled AxiosError with a sanitized ApiError and
 * redact auth headers / config / bodies / bearer tokens from extra, contexts
 * and mechanism data (sentryScrub.ts).
 */
export function sentryBeforeSend<T extends Sentry.ErrorEvent>(event: T, hint: EventHint): T | null {
  const kept = filterExpectedNetworkNoise(event, hint, () =>
    Sentry.addBreadcrumb({
      category: 'sentry.filter',
      message: 'Dropped expected client network error',
      level: 'info',
    })
  );
  return kept ? scrubSentryEvent(kept, hint) : null;
}

/**
 * #736: breadcrumbs (including the #701 `sentry.filter` one) get the same
 * scrub, so tokens in an http breadcrumb's url/data never reach Sentry.
 */
export function sentryBeforeBreadcrumb<T extends Sentry.Breadcrumb>(breadcrumb: T): T | null {
  return scrubBreadcrumb(breadcrumb);
}

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
      beforeSend: sentryBeforeSend,
      beforeBreadcrumb: sentryBeforeBreadcrumb,
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
