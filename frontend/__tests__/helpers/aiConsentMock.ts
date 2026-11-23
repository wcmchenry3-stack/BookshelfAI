/**
 * The default `useAiConsent` under Jest (#1023, installed by jest.setup.ts):
 * consent already given, so tests written before the consent gate keep
 * scanning as they did (like TURNSTILE_REQUIRED=false on the backend). Tests
 * of the consent flow either change `aiConsentMock` (`status`, `answer`, or
 * `jest.spyOn` a method) and call `resetAiConsentMock()` afterwards, or
 * `jest.unmock('../../hooks/useAiConsent')` and render the real
 * AiConsentProvider.
 *
 * Plain functions, not `jest.fn()`: a file that calls `jest.resetAllMocks()`
 * would otherwise wipe their answers and every photo would be held.
 *
 * Must NOT import app code (it is loaded by a jest.mock factory).
 */
type Status = 'unknown' | 'given' | 'none';

const GIVEN_AT = '2026-11-01T00:00:00+00:00';

export const aiConsentMock = {
  status: 'given' as Status,
  givenAt: GIVEN_AT as string | null,
  /** What `ensureConsent` and `checkConsent` resolve to. */
  answer: true,
  ensureConsent: async (_options?: { allowUnknown?: boolean }) => aiConsentMock.answer,
  checkConsent: async () => aiConsentMock.answer,
  markRequired: () => {},
  withdraw: async () => true,
};

const defaults = { ...aiConsentMock };

/** Back to "consent given" (call from an afterEach when a test changed it). */
export function resetAiConsentMock() {
  Object.assign(aiConsentMock, defaults);
}

export function aiConsentModule() {
  return { useAiConsent: () => aiConsentMock };
}
