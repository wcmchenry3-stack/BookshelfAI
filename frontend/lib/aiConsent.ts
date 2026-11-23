/**
 * Consent to send scan photos to the AI provider (#1023, Apple 5.1.2(i)).
 *
 * Stored per account on the server (`users.ai_consent_at` /
 * `ai_consent_withdrawn_at`): `GET /auth/me` reports it, `POST
 * /users/me/ai-consent` gives it and `DELETE` withdraws it. The server picks
 * the timestamps; the app never sends one. With `AI_CONSENT_REQUIRED` on, the
 * server refuses `POST /scan` without consent (`403 ai_consent_required`,
 * classified in lib/scanErrors.ts).
 */
import { api } from './api';
import { isIdempotencyOutcomeUnknown } from './idempotency';

/**
 * The hosted privacy policy, linked from the consent prompt. The URL recorded
 * for App Store Connect (docs/store-compliance.md, 5.1.1 Privacy policy link).
 */
export const PRIVACY_POLICY_URL = 'https://wcmchenry3-stack.github.io/book_app/privacy.html';

const PATH = '/users/me/ai-consent';

/** How long a read of the consent state waits before giving up (unknown). */
const AI_CONSENT_FETCH_TIMEOUT_MS = 8000;

export interface AiConsentState {
  /** Whether consent stands: given and not withdrawn since. */
  given: boolean;
  /** When it was last given (ISO, server time), or null. */
  givenAt: string | null;
}

/** The consent state from a `GET /auth/me` or consent response body; null when absent. */
export function aiConsentFromBody(data: unknown): AiConsentState | null {
  if (!data || typeof data !== 'object') return null;
  const body = data as { ai_consent?: unknown; ai_consent_at?: unknown };
  if (typeof body.ai_consent !== 'boolean') return null;
  return {
    given: body.ai_consent,
    givenAt: typeof body.ai_consent_at === 'string' ? body.ai_consent_at : null,
  };
}

/** The state from `GET /auth/me`, or null when it cannot be read (offline, an error). Never rejects. */
export async function loadAiConsent(): Promise<AiConsentState | null> {
  try {
    const { data } = await api.get('/auth/me', { timeout: AI_CONSENT_FETCH_TIMEOUT_MS });
    return aiConsentFromBody(data);
  } catch {
    return null;
  }
}

function stateOrThrow(data: unknown): AiConsentState {
  const state = aiConsentFromBody(data);
  if (!state) throw new Error('unexpected ai-consent response');
  return state;
}

/**
 * The write was applied but its response was lost (`409
 * idempotency_outcome_unknown`, #685): read the state back instead of
 * failing. Rethrows anything else, or when the read fails too.
 */
async function stateAfterUnknownOutcome(err: unknown): Promise<AiConsentState> {
  if (!isIdempotencyOutcomeUnknown(err)) throw err;
  const state = await loadAiConsent();
  if (!state) throw err;
  return state;
}

/** Records consent on the server; resolves to the stored state. Rejects on failure. */
export async function grantAiConsent(): Promise<AiConsentState> {
  try {
    const { data } = await api.post(PATH);
    return stateOrThrow(data);
  } catch (err) {
    return stateAfterUnknownOutcome(err);
  }
}

/** Withdraws consent on the server; resolves to the stored state. Rejects on failure. */
export async function withdrawAiConsent(): Promise<AiConsentState> {
  try {
    const { data } = await api.delete(PATH);
    return stateOrThrow(data);
  } catch (err) {
    return stateAfterUnknownOutcome(err);
  }
}
