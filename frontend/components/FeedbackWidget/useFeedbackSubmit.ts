/**
 * In-app feedback goes to Sentry User Feedback (private, no network handling
 * here: the SDK queues the envelope and retries when the device is online).
 * Throttled per install: 5 submissions per 10 minutes.
 */
import { useState } from 'react';
import { Sentry } from '../../lib/sentry';
import { SessionLogger } from './SessionLogger';

export type FeedbackType = 'bug' | 'feature';

export interface FeedbackPayload {
  description: string;
  type: FeedbackType;
}

export type SubmitStatus = 'idle' | 'submitting' | 'success' | 'error';

export interface SubmitError {
  /** `unavailable`: Sentry is not initialised in this build (no DSN). */
  kind: 'rate_limit' | 'unavailable';
  retryAfterSeconds?: number;
}

export interface UseFeedbackSubmit {
  status: SubmitStatus;
  error: SubmitError | null;
  /** Resolves true when the feedback was handed to Sentry. */
  submit: (payload: FeedbackPayload) => Promise<boolean>;
  reset: () => void;
}

export const DESCRIPTION_MAX = 2000;
export const FEEDBACK_RATE_LIMIT_MAX = 5;
export const FEEDBACK_RATE_LIMIT_WINDOW_MS = 10 * 60 * 1000;

// Module-level so the throttle survives the widget being closed and reopened.
let recentSubmissions: number[] = [];

/** Clear the client throttle — used in tests. */
export function _resetFeedbackThrottle(): void {
  recentSubmissions = [];
}

function retryAfterSeconds(now: number): number | null {
  recentSubmissions = recentSubmissions.filter((ts) => now - ts < FEEDBACK_RATE_LIMIT_WINDOW_MS);
  if (recentSubmissions.length < FEEDBACK_RATE_LIMIT_MAX) return null;
  const oldest = recentSubmissions[0] ?? now;
  return Math.ceil((FEEDBACK_RATE_LIMIT_WINDOW_MS - (now - oldest)) / 1000);
}

export function useFeedbackSubmit(): UseFeedbackSubmit {
  const [status, setStatus] = useState<SubmitStatus>('idle');
  const [error, setError] = useState<SubmitError | null>(null);

  async function submit(payload: FeedbackPayload): Promise<boolean> {
    setError(null);

    // Sentry is not initialised without a DSN, so there is nowhere to send it.
    if (!Sentry.getClient()) {
      setStatus('error');
      setError({ kind: 'unavailable' });
      return false;
    }

    const now = Date.now();
    const retryAfter = retryAfterSeconds(now);
    if (retryAfter !== null) {
      setStatus('error');
      setError({ kind: 'rate_limit', retryAfterSeconds: retryAfter });
      return false;
    }

    setStatus('submitting');

    // Logs help debug a bug report; a feature request has no use for them.
    const logs = payload.type === 'bug' ? SessionLogger.getLogs() : '';
    try {
      Sentry.captureFeedback(
        {
          message: payload.description.slice(0, DESCRIPTION_MAX),
          source: 'in_app_feedback',
          tags: { 'feedback.type': payload.type },
        },
        logs
          ? {
              attachments: [
                { filename: 'session-logs.txt', data: logs, contentType: 'text/plain' },
              ],
            }
          : undefined
      );
      recentSubmissions.push(now);
      setStatus('success');
      return true;
    } catch {
      setStatus('error');
      setError({ kind: 'unavailable' });
      return false;
    }
  }

  function reset(): void {
    setStatus('idle');
    setError(null);
  }

  return { status, error, submit, reset };
}
