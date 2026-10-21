import { useState } from 'react';
import { Sentry } from '../../lib/sentry';
import { SessionLogger } from './SessionLogger';

export type FeedbackType = 'bug' | 'feature';

export interface FeedbackPayload {
  title: string;
  description: string;
  type: FeedbackType;
}

export type SubmitStatus = 'idle' | 'submitting' | 'success' | 'error';

export interface SubmitError {
  kind: 'rate_limit' | 'unknown';
  retryAfterSeconds?: number;
}

export interface UseFeedbackSubmit {
  status: SubmitStatus;
  error: SubmitError | null;
  submit: (payload: FeedbackPayload) => Promise<void>;
  reset: () => void;
}

export const TITLE_MAX = 120;
export const DESCRIPTION_MAX = 2000;
export const SUBMIT_COOLDOWN_MS = 60_000;

// Module-level so the throttle survives the widget being closed and reopened.
let lastSubmitAt = 0;

/** Clear the client throttle — used in tests. */
export function _resetFeedbackThrottle(): void {
  lastSubmitAt = 0;
}

export function useFeedbackSubmit(): UseFeedbackSubmit {
  const [status, setStatus] = useState<SubmitStatus>('idle');
  const [error, setError] = useState<SubmitError | null>(null);

  async function submit(payload: FeedbackPayload): Promise<void> {
    const waitMs = lastSubmitAt + SUBMIT_COOLDOWN_MS - Date.now();
    if (waitMs > 0) {
      setStatus('error');
      setError({ kind: 'rate_limit', retryAfterSeconds: Math.ceil(waitMs / 1000) });
      return;
    }

    // Sentry is not initialised without a DSN (and in some web builds), so
    // there is nowhere to send the feedback.
    if (!Sentry.getClient()) {
      setStatus('error');
      setError({ kind: 'unknown' });
      return;
    }

    setStatus('submitting');
    setError(null);

    const title = payload.title.slice(0, TITLE_MAX);
    const description = payload.description.slice(0, DESCRIPTION_MAX);
    const logs = SessionLogger.getLogs();

    try {
      Sentry.captureFeedback(
        {
          message: `${title}\n\n${description}`,
          tags: { feedback_type: payload.type },
        },
        {
          attachments: logs
            ? [{ filename: 'session-logs.txt', data: logs, contentType: 'text/plain' }]
            : undefined,
        }
      );
    } catch {
      setStatus('error');
      setError({ kind: 'unknown' });
      return;
    }

    lastSubmitAt = Date.now();
    setStatus('success');
  }

  function reset(): void {
    setStatus('idle');
    setError(null);
  }

  return { status, error, submit, reset };
}
