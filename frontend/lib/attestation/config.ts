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

/** SecureStore key prefix of the registered App Attest key id (iOS). */
const APP_ATTEST_KEY_STORAGE_KEY ='bookshelf_app_attest_key_id';

/** A registration outlives the scan that started it, but not by more than this. */
export const REGISTRATION_TIMEOUT_MS = 15_000;

/**
 * The SecureStore key of one account's App Attest key id. The server keeps
 * keys per user (`device_keys.user_id`), so each account on a device has its
 * own, and a returning user keeps theirs.
 */
export function appAttestKeyStorageKey(userId: string): string {
  return `${APP_ATTEST_KEY_STORAGE_KEY}.${userId}`;
}

const USER_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * The user id (`sub`, a UUID, backend/app/auth/jwt.py) of an access token, or
 * null. Not verified: it only picks which stored key to use; the server
 * authenticates the request itself.
 */
export function userIdFromAccessToken(token: string | null | undefined): string | null {
  const payload = token?.split('.')[1];
  if (!payload) return null;
  try {
    const b64 = payload.replace(/-/g, '+').replace(/_/g, '/');
    const json = JSON.parse(atob(b64.padEnd(Math.ceil(b64.length / 4) * 4, '='))) as {
      sub?: unknown;
    };
    return typeof json.sub === 'string' && USER_ID_PATTERN.test(json.sub) ? json.sub : null;
  } catch {
    return null;
  }
}

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
