/**
 * Device attestation on POST /scan (#870, server side #753).
 *
 * The form fields and endpoints below are the ones the server reads
 * (backend/app/api/scan.py, backend/app/api/attestation.py). Nothing here is
 * secret: the cloud project number ships in the app bundle.
 *
 *   EXPO_PUBLIC_PLAY_INTEGRITY_CLOUD_PROJECT_NUMBER  Android only: the Google
 *       Cloud project number linked to the app in Play Console (App integrity).
 *       Unset or not a number: Android sends no attestation.
 */
import { Platform } from 'react-native';

/** The most one scan waits for attestation before it is sent without it. */
export const ATTESTATION_TIMEOUT_MS = 5_000;

/** `POST /scan` form fields (aliases in backend/app/api/scan.py). */
export const ATTESTATION_FIELDS = {
  platform: 'attestation-platform',
  keyId: 'attestation-key-id',
  assertion: 'attestation-assertion',
  token: 'attestation-token',
  nonce: 'attestation-nonce',
} as const;

export const NONCE_PATH = '/attestation/nonce';
export const REGISTER_PATH = '/attestation/register';

/** SecureStore key of the registered App Attest key id (iOS). */
export const APP_ATTEST_KEY_STORAGE_KEY = 'bookshelf_app_attest_key_id';

/** The server's 403 when it was sent attestation data and rejected it. */
export const ATTESTATION_REJECTED_DETAIL = 'Attestation verification failed';

/** The server's 503 detail while attestation is not set up there. */
export const NOT_CONFIGURED_DETAIL = 'attestation_not_configured';

export type AttestationPlatform = 'ios' | 'android';

/**
 * The platform to attest on, or null to send nothing. Dev builds (which
 * includes every simulator / emulator run from Metro) and web skip it: the
 * server has no "dev" platform, and attestation data that fails verification
 * is refused once ATTESTATION_REQUIRED is on, while no attestation data falls
 * back to Turnstile.
 */
export function attestationPlatform(): AttestationPlatform | null {
  if (__DEV__) return null;
  return Platform.OS === 'ios' || Platform.OS === 'android' ? Platform.OS : null;
}

/** Play Integrity's cloud project number, or null when unset or malformed. */
export function playIntegrityCloudProjectNumber(): string | null {
  const raw = process.env.EXPO_PUBLIC_PLAY_INTEGRITY_CLOUD_PROJECT_NUMBER?.trim();
  return raw && /^\d{1,20}$/.test(raw) ? raw : null;
}

/** A server nonce is `<base64url>.<base64url>` (backend/app/core/attestation.py). */
export function isPlausibleNonce(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    value.length <= 500 &&
    /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(value)
  );
}
