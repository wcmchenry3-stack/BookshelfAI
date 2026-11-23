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

import { ACCESS_TOKEN_KEY, api } from '../api';
import { toApiError } from '../apiError';
import { Sentry } from '../sentry';
import * as storage from '../storage';
import {
  appAttestKeyStorageKey,
  ATTESTATION_FIELDS,
  ATTESTATION_REJECTED_DETAIL,
  ATTESTATION_TIMEOUT_MS,
  attestationPlatform,
  isPlausibleNonce,
  NONCE_PATH,
  NOT_CONFIGURED_DETAIL,
  playIntegrityCloudProjectNumber,
  REGISTER_PATH,
  REGISTRATION_TIMEOUT_MS,
  type AttestationPlatform,
  userIdFromAccessToken,
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
  | 'aborted'
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

interface Registration {
  userId: string;
  controller: AbortController;
  promise: Promise<string>;
}

// Per app session and account (reset on sign-out and on an account change).
let sessionUser: string | null = null;
let disabled: Reason | null = null;
let registration: Registration | null = null;
let providerReady: Promise<void> | null = null;
const reported = new Set<string>();

/**
 * Forget this session's attestation state: the account, the latch, the
 * prepared provider, the Sentry dedupe set, and any registration still
 * running (aborted, so it never stores a key). Called on sign-out
 * (AuthContext) and whenever the signed-in account changes.
 */
export function resetAttestationState(): void {
  registration?.controller.abort();
  registration = null;
  sessionUser = null;
  disabled = null;
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
  if (err instanceof AttestationError) return err;
  const code = nativeCode(err);
  if (code && UNSUPPORTED_CODES.has(code)) return new AttestationError('unsupported', true, code);
  return new AttestationError('native_error', false, code);
}

/** Stop between steps once the work was cancelled (timeout, abort, reset). */
function live(signal: AbortSignal): void {
  if (signal.aborted) throw new AttestationError('aborted');
}

/** An API failure as an AttestationError (`fallback` unless the server said why). */
function serverError(err: unknown, fallback: Reason, signal: AbortSignal): AttestationError {
  if (signal.aborted) return new AttestationError('aborted');
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

/**
 * Run `work` on its own signal, aborted when the caller's signal aborts or
 * the attestation timeout passes, so the steps still to come (a nonce, a Play
 * Integrity request, an assertion) are not run for a scan that went without.
 */
function bounded<T>(work: (signal: AbortSignal) => Promise<T>, caller?: AbortSignal): Promise<T> {
  const controller = new AbortController();
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => done(() => reject(new AttestationError('aborted')));
    const timer = setTimeout(
      () => done(() => reject(new AttestationError('timeout'))),
      ATTESTATION_TIMEOUT_MS
    );
    function done(settle: () => void) {
      clearTimeout(timer);
      caller?.removeEventListener('abort', onAbort);
      controller.abort();
      settle();
    }
    work(controller.signal).then(
      (value) => done(() => resolve(value)),
      (err) => done(() => reject(err))
    );
    if (caller?.aborted) onAbort();
    else caller?.addEventListener('abort', onAbort);
  });
}

async function fetchNonce(platform: AttestationPlatform, signal: AbortSignal): Promise<string> {
  live(signal);
  crumb('nonce requested', platform);
  let nonce: unknown;
  try {
    const res = await api.post<{ nonce?: unknown }>(NONCE_PATH, undefined, {
      signal,
      timeout: ATTESTATION_TIMEOUT_MS,
    });
    nonce = res.data?.nonce;
  } catch (err) {
    throw serverError(err, 'nonce_failed', signal);
  }
  if (!isPlausibleNonce(nonce)) throw new AttestationError('nonce_failed', false, 'malformed');
  return nonce;
}

/** The signed-in account, from the stored access token; null when signed out. */
async function currentUserId(): Promise<string | null> {
  try {
    return userIdFromAccessToken(await storage.getItem(ACCESS_TOKEN_KEY));
  } catch {
    return null;
  }
}

async function storedKeyId(userId: string): Promise<string | null> {
  try {
    return await storage.getItem(appAttestKeyStorageKey(userId));
  } catch {
    throw new AttestationError('storage_failed');
  }
}

/** Forget an account's App Attest key, so its next scan registers a new one. */
async function forgetKey(userId: string): Promise<void> {
  try {
    await storage.deleteItem(appAttestKeyStorageKey(userId));
  } catch {
    // Left in place: the next failure tries again.
  }
  crumb('stored key cleared', 'ios');
}

/** Generate, attest and register a new App Attest key for `userId`; resolves to its id. */
async function register(userId: string, signal: AbortSignal): Promise<string> {
  const nonce = await fetchNonce('ios', signal);
  let keyId: string;
  let attestation: string;
  try {
    keyId = await AppIntegrity.generateKeyAsync();
    live(signal);
    attestation = await AppIntegrity.attestKeyAsync(keyId, nonce);
  } catch (err) {
    throw nativeError(err);
  }
  live(signal);
  crumb('key registration requested', 'ios');
  try {
    await api.post(
      REGISTER_PATH,
      { key_id: keyId, attestation_b64: attestation, nonce },
      { signal, timeout: ATTESTATION_TIMEOUT_MS }
    );
  } catch (err) {
    throw serverError(err, 'register_failed', signal);
  }
  // Reset (sign-out, another account) or timed out meanwhile: never stored.
  live(signal);
  try {
    await storage.setItem(appAttestKeyStorageKey(userId), keyId);
  } catch {
    throw new AttestationError('storage_failed');
  }
  crumb('key registered', 'ios');
  return keyId;
}

/**
 * One registration at a time, shared by the scans that wait for it. It runs
 * on its own signal and timeout, so one scan's abort or timeout does not
 * cancel it for the others (a registration that finishes is kept for the
 * next scan); a reset or another account aborts it.
 */
function registeredKeyId(userId: string): Promise<string> {
  if (registration?.userId === userId) return registration.promise;
  registration?.controller.abort();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REGISTRATION_TIMEOUT_MS);
  const entry: Registration = {
    userId,
    controller,
    promise: register(userId, controller.signal).finally(() => {
      clearTimeout(timer);
      if (registration === entry) registration = null;
    }),
  };
  // A scan that stopped waiting must not leave the rejection unhandled.
  entry.promise.catch(() => {});
  registration = entry;
  return entry.promise;
}

async function iosFields(userId: string, signal: AbortSignal): Promise<AttestationFields> {
  // False on the simulator and on devices without the Secure Enclave service.
  if (!AppIntegrity.isSupported) throw new AttestationError('unsupported', true);
  const keyId = (await storedKeyId(userId)) ?? (await registeredKeyId(userId));
  const nonce = await fetchNonce('ios', signal);
  live(signal);
  let assertion: string;
  try {
    assertion = await AppIntegrity.generateAssertionAsync(keyId, nonce);
  } catch (err) {
    if (nativeCode(err) === INVALID_KEY) {
      await forgetKey(userId);
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

async function androidFields(signal: AbortSignal): Promise<AttestationFields> {
  const cloudProjectNumber = playIntegrityCloudProjectNumber();
  if (!cloudProjectNumber) throw new AttestationError('not_configured', true);
  await preparedProvider(cloudProjectNumber);
  const nonce = await fetchNonce('android', signal);
  live(signal);
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
  const failure = nativeError(err);
  if (failure.latch) disabled = failure.reason;
  if (failure.reason === 'aborted') return;
  // Expected states, not failures, so a breadcrumb only: not set up (no cloud
  // project number, or the server has no nonce secret / Apple app id: an owner
  // step) and devices that cannot attest (simulator, no App Attest, no Play
  // Store or Play services), which would otherwise warn on every launch.
  if (failure.reason === 'not_configured' || failure.reason === 'unsupported') {
    const state = failure.reason === 'unsupported' ? 'unsupported' : 'not configured';
    crumb(`attestation ${state}`, platform);
    return;
  }
  report(platform, failure.reason, failure.code);
}

/**
 * Whether `getAttestationFields()` would do any work (and so may await for up
 * to ATTESTATION_TIMEOUT_MS): false on web, in dev builds and once attestation
 * is off for this session.
 */
export function attestationActive(): boolean {
  return attestationPlatform() !== null && disabled === null;
}

/**
 * The attestation fields for one POST /scan, or null to send none (web, dev
 * builds, signed out, unsupported or unconfigured, or any failure). Never
 * rejects.
 */
export async function getAttestationFields(
  signal?: AbortSignal
): Promise<AttestationFields | null> {
  const platform = attestationPlatform();
  if (!platform || signal?.aborted) return null;
  const userId = await currentUserId();
  if (!userId || signal?.aborted) return null;
  // Another account than the one this session's state belongs to.
  if (sessionUser !== userId) {
    resetAttestationState();
    sessionUser = userId;
  }
  if (disabled) return null;
  try {
    return await bounded(
      (work) => (platform === 'ios' ? iosFields(userId, work) : androidFields(work)),
      signal
    );
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
 * The server refused the attestation that was sent. Logged once.
 * iOS: the account's key is forgotten (unknown to the server, or a replayed
 * counter), so its next scan registers a new one. Android: attestation is off
 * for the rest of the session, so later scans are not each posted twice (the
 * rejected post and the re-send both count against the scan limits).
 */
export async function onAttestationRejected(fields: AttestationFields): Promise<void> {
  if (fields.platform === 'ios') {
    const userId = await currentUserId();
    if (userId) await forgetKey(userId);
  } else {
    disabled = 'server_rejected';
  }
  report(fields.platform, 'server_rejected', 'http_403');
}
