/**
 * App Attest (iOS) and Play Integrity (Android) for POST /scan (#870).
 *
 * `getAttestationFields()` runs before every scan POST (retries included, so
 * every POST has its own nonce) and resolves to the fields to send, or null
 * to send none. It never rejects and never waits longer than
 * ATTESTATION_TIMEOUT_MS: any failure is logged and the scan goes without
 * attestation, so the server's Turnstile fallback decides.
 *
 * Nothing secret (nonce, key id, assertion, token, attestation object) is
 * ever put in a breadcrumb, a Sentry event or a log line.
 */
import * as AppIntegrity from '@expo/app-integrity';

import { api } from '../api';
import { toApiError } from '../apiError';
import { Sentry } from '../sentry';
import * as storage from '../storage';
import {
  APP_ATTEST_KEY_STORAGE_KEY,
  ATTESTATION_FIELDS,
  ATTESTATION_REJECTED_DETAIL,
  ATTESTATION_TIMEOUT_MS,
  attestationPlatform,
  isPlausibleNonce,
  NONCE_PATH,
  NOT_CONFIGURED_DETAIL,
  playIntegrityCloudProjectNumber,
  REGISTER_PATH,
  type AttestationPlatform,
} from './config';

export interface AttestationFields {
  platform: AttestationPlatform;
  nonce: string;
  /** iOS: the registered App Attest key id. */
  keyId?: string;
  /** iOS: the assertion over SHA-256(nonce), base64 as DCAppAttestService returns it. */
  assertion?: string;
  /** Android: the Play Integrity (Standard API) token, requested with the nonce as request hash. */
  token?: string;
}

type Reason =
  | 'timeout'
  | 'unsupported'
  | 'not_configured'
  | 'nonce_failed'
  | 'register_failed'
  | 'register_rejected'
  | 'key_invalid'
  | 'native_error'
  | 'storage_failed'
  | 'server_rejected'
  | 'unknown';

class AttestationError extends Error {
  constructor(
    readonly reason: Reason,
    /** Stop trying for the rest of this app session (nothing will change until a restart). */
    readonly latch = false,
    /** A fixed native error code or `http_<status>`; never free text. */
    readonly code?: string
  ) {
    super(reason);
    this.name = 'AttestationError';
  }
}

// Native error codes from @expo/app-integrity (ios/IntegrityErrorCodes.swift,
// android/.../IntegrityErrorCodes.kt).
const INVALID_KEY = 'ERR_APP_INTEGRITY_INVALID_KEY';
/** The device or build cannot attest at all: stop for this session. */
const UNSUPPORTED_CODES: ReadonlySet<string> = new Set([
  'ERR_APP_INTEGRITY_FEATURE_UNSUPPORTED',
  'ERR_APP_INTEGRITY_API_NOT_AVAILABLE',
  'ERR_APP_INTEGRITY_PLAY_SERVICES_NOT_FOUND',
  'ERR_APP_INTEGRITY_PLAY_STORE_NOT_FOUND',
  'ERR_APP_INTEGRITY_INVALID_PROJECT_NUMBER',
]);
/** The prepared Play Integrity provider is unusable: prepare a new one next time. */
const RESET_PROVIDER_CODES: ReadonlySet<string> = new Set([
  'ERR_APP_INTEGRITY_PROVIDER_INVALID',
  'ERR_APP_INTEGRITY_PROVIDER_NOT_PREPARED',
]);

// Per app session.
let disabled: Reason | null = null;
let registering: Promise<string> | null = null;
let providerReady: Promise<void> | null = null;
const reported = new Set<string>();

/** Test hook: forget this session's state (latch, in-flight work, reports). */
export function resetAttestationState(): void {
  disabled = null;
  registering = null;
  providerReady = null;
  reported.clear();
}

function crumb(message: string, platform: AttestationPlatform): void {
  Sentry.addBreadcrumb({ category: 'attestation', message, level: 'info', data: { platform } });
}

function nativeCode(err: unknown): string | undefined {
  const code = (err as { code?: unknown } | null)?.code;
  return typeof code === 'string' && /^[A-Z0-9_]{1,80}$/.test(code) ? code : undefined;
}

function nativeError(err: unknown): AttestationError {
  const code = nativeCode(err);
  if (code && UNSUPPORTED_CODES.has(code)) return new AttestationError('unsupported', true, code);
  return new AttestationError('native_error', false, code);
}

/** An API failure as an AttestationError (`fallback` unless the server said why). */
function serverError(err: unknown, fallback: Reason): AttestationError {
  const { status, code } = toApiError(err);
  if (status === 503 && code === NOT_CONFIGURED_DETAIL) {
    return new AttestationError('not_configured', true);
  }
  // The server's verifier refused the key (always, until its real Apple
  // verifier ships): Apple asks not to retry attestKey for the same reason.
  if (status === 400 && code === 'attestation_invalid') {
    return new AttestationError('register_rejected', true, 'http_400');
  }
  return new AttestationError(fallback, false, status ? `http_${status}` : 'network');
}

/** Bound `work` by the attestation timeout and the caller's signal. */
function bounded<T>(work: Promise<T>, signal?: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => done(() => reject(new AttestationError('unknown')));
    const timer = setTimeout(
      () => done(() => reject(new AttestationError('timeout'))),
      ATTESTATION_TIMEOUT_MS
    );
    function done(settle: () => void) {
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
      settle();
    }
    // Work still running past the bound is left to finish (a registration
    // that completes is kept for the next scan); its result is ignored here.
    work.then(
      (value) => done(() => resolve(value)),
      (err) => done(() => reject(err))
    );
    if (signal?.aborted) onAbort();
    else signal?.addEventListener('abort', onAbort);
  });
}

async function fetchNonce(platform: AttestationPlatform, signal?: AbortSignal): Promise<string> {
  crumb('nonce requested', platform);
  let nonce: unknown;
  try {
    const res = await api.post<{ nonce?: unknown }>(NONCE_PATH, undefined, {
      signal,
      timeout: ATTESTATION_TIMEOUT_MS,
    });
    nonce = res.data?.nonce;
  } catch (err) {
    throw serverError(err, 'nonce_failed');
  }
  if (!isPlausibleNonce(nonce)) throw new AttestationError('nonce_failed', false, 'malformed');
  return nonce;
}

async function storedKeyId(): Promise<string | null> {
  try {
    return await storage.getItem(APP_ATTEST_KEY_STORAGE_KEY);
  } catch {
    throw new AttestationError('storage_failed');
  }
}

/** Forget the stored App Attest key, so the next scan registers a new one. */
async function forgetKey(): Promise<void> {
  try {
    await storage.deleteItem(APP_ATTEST_KEY_STORAGE_KEY);
  } catch {
    // Left in place: the next failure tries again.
  }
  crumb('stored key cleared', 'ios');
}

/** Generate, attest and register a new App Attest key; resolves to its id. */
async function register(signal?: AbortSignal): Promise<string> {
  const nonce = await fetchNonce('ios', signal);
  let keyId: string;
  let attestation: string;
  try {
    keyId = await AppIntegrity.generateKeyAsync();
    attestation = await AppIntegrity.attestKeyAsync(keyId, nonce);
  } catch (err) {
    throw nativeError(err);
  }
  crumb('key registration requested', 'ios');
  try {
    await api.post(
      REGISTER_PATH,
      { key_id: keyId, attestation_b64: attestation, nonce },
      { signal, timeout: ATTESTATION_TIMEOUT_MS }
    );
  } catch (err) {
    throw serverError(err, 'register_failed');
  }
  try {
    await storage.setItem(APP_ATTEST_KEY_STORAGE_KEY, keyId);
  } catch {
    throw new AttestationError('storage_failed');
  }
  crumb('key registered', 'ios');
  return keyId;
}

/** One registration at a time, shared by every caller while it runs. */
function registeredKeyId(signal?: AbortSignal): Promise<string> {
  registering ??= register(signal).finally(() => {
    registering = null;
  });
  return registering;
}

async function iosFields(signal?: AbortSignal): Promise<AttestationFields> {
  // False on the simulator and on devices without the Secure Enclave service.
  if (!AppIntegrity.isSupported) throw new AttestationError('unsupported', true);
  const keyId = (await storedKeyId()) ?? (await registeredKeyId(signal));
  const nonce = await fetchNonce('ios', signal);
  let assertion: string;
  try {
    assertion = await AppIntegrity.generateAssertionAsync(keyId, nonce);
  } catch (err) {
    if (nativeCode(err) === INVALID_KEY) {
      await forgetKey();
      throw new AttestationError('key_invalid', false, INVALID_KEY);
    }
    throw nativeError(err);
  }
  crumb('assertion generated', 'ios');
  return { platform: 'ios', nonce, keyId, assertion };
}

function preparedProvider(cloudProjectNumber: string): Promise<void> {
  providerReady ??= AppIntegrity.prepareIntegrityTokenProviderAsync(cloudProjectNumber).catch(
    (err: unknown) => {
      providerReady = null;
      throw nativeError(err);
    }
  );
  return providerReady;
}

async function androidFields(signal?: AbortSignal): Promise<AttestationFields> {
  const cloudProjectNumber = playIntegrityCloudProjectNumber();
  if (!cloudProjectNumber) throw new AttestationError('not_configured', true);
  await preparedProvider(cloudProjectNumber);
  const nonce = await fetchNonce('android', signal);
  let token: string;
  try {
    token = await AppIntegrity.requestIntegrityCheckAsync(nonce);
  } catch (err) {
    if (RESET_PROVIDER_CODES.has(nativeCode(err) ?? '')) providerReady = null;
    throw nativeError(err);
  }
  crumb('integrity token received', 'android');
  return { platform: 'android', nonce, token };
}

/** Log a failure to Sentry once per session per platform, reason and code. */
function report(platform: AttestationPlatform, reason: Reason, code?: string): void {
  const key = `${platform}:${reason}:${code ?? ''}`;
  if (reported.has(key)) return;
  reported.add(key);
  Sentry.captureMessage('Device attestation failed; scan sent without it', {
    level: 'warning',
    tags: {
      feature: 'scan',
      error: platform === 'ios' ? 'attestation_failure' : 'play_integrity_unavailable',
      'attestation.platform': platform,
      'attestation.reason': reason,
      ...(code ? { 'attestation.code': code } : {}),
    },
    fingerprint: ['scan-attestation', platform, reason, code ?? ''],
  });
}

function noteFailure(platform: AttestationPlatform, err: unknown): void {
  const failure = err instanceof AttestationError ? err : nativeError(err);
  if (failure.latch) disabled = failure.reason;
  // Not set up (no cloud project number, or the server has no nonce secret
  // or Apple app id): an owner step, not a device failure. Breadcrumb only.
  if (failure.reason === 'not_configured') {
    crumb('attestation not configured', platform);
    return;
  }
  report(platform, failure.reason, failure.code);
}

/**
 * The attestation fields for one POST /scan, or null to send none (web, dev
 * builds, unsupported or unconfigured, or any failure). Never rejects.
 */
export async function getAttestationFields(
  signal?: AbortSignal
): Promise<AttestationFields | null> {
  const platform = attestationPlatform();
  if (!platform || disabled || signal?.aborted) return null;
  try {
    return await bounded(platform === 'ios' ? iosFields(signal) : androidFields(signal), signal);
  } catch (err) {
    // Cancelled (sign-out, job aborted): nothing to log.
    if (signal?.aborted) return null;
    noteFailure(platform, err);
    return null;
  }
}

/** Add the fields the server reads (backend/app/api/scan.py) to a scan form. */
export function appendAttestationFields(form: FormData, fields: AttestationFields | null): void {
  if (!fields) return;
  form.append(ATTESTATION_FIELDS.platform, fields.platform);
  form.append(ATTESTATION_FIELDS.nonce, fields.nonce);
  if (fields.keyId) form.append(ATTESTATION_FIELDS.keyId, fields.keyId);
  if (fields.assertion) form.append(ATTESTATION_FIELDS.assertion, fields.assertion);
  if (fields.token) form.append(ATTESTATION_FIELDS.token, fields.token);
  crumb('scan submitted with attestation', fields.platform);
}

/** The server's `403 "Attestation verification failed"` (only with ATTESTATION_REQUIRED on). */
export function isAttestationRejection(err: unknown): boolean {
  const { status, code } = toApiError(err);
  return status === 403 && code === ATTESTATION_REJECTED_DETAIL;
}

/**
 * The server refused the attestation that was sent. iOS: the key is forgotten
 * (unknown to the server, e.g. registered by another account on this device,
 * or a replayed counter), so the next scan registers a new one. Logged once.
 */
export async function onAttestationRejected(fields: AttestationFields): Promise<void> {
  if (fields.platform === 'ios') await forgetKey();
  report(fields.platform, 'server_rejected', 'http_403');
}
