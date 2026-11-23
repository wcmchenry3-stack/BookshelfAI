/**
 * Device attestation client (#870): App Attest on iOS, Play Integrity on
 * Android, nothing on web or in dev builds, and every failure falls back to
 * "no attestation" within 5 s without leaking key material to Sentry.
 */
import { Platform } from 'react-native';

import { api } from '../../lib/api';
import {
  appendAttestationFields,
  attestationActive,
  getAttestationFields,
  isAttestationRejection,
  onAttestationRejected,
  resetAttestationState,
} from '../../lib/attestation/attestation';
import {
  appAttestKeyStorageKey,
  ATTESTATION_TIMEOUT_MS,
  playIntegrityCloudProjectNumber,
  userIdFromAccessToken,
} from '../../lib/attestation/config';
import { Sentry } from '../../lib/sentry';
import { scrubBreadcrumb } from '../../lib/sentryScrub';
import * as storage from '../../lib/storage';

const mockIntegrity = {
  supported: true,
  generateKeyAsync: jest.fn(),
  attestKeyAsync: jest.fn(),
  generateAssertionAsync: jest.fn(),
  prepareIntegrityTokenProviderAsync: jest.fn(),
  requestIntegrityCheckAsync: jest.fn(),
};
jest.mock('@expo/app-integrity', () => ({
  get isSupported() {
    return mockIntegrity.supported;
  },
  generateKeyAsync: (...a: unknown[]) => mockIntegrity.generateKeyAsync(...a),
  attestKeyAsync: (...a: unknown[]) => mockIntegrity.attestKeyAsync(...a),
  generateAssertionAsync: (...a: unknown[]) => mockIntegrity.generateAssertionAsync(...a),
  prepareIntegrityTokenProviderAsync: (...a: unknown[]) =>
    mockIntegrity.prepareIntegrityTokenProviderAsync(...a),
  requestIntegrityCheckAsync: (...a: unknown[]) => mockIntegrity.requestIntegrityCheckAsync(...a),
}));
jest.mock('../../lib/api', () => ({
  ACCESS_TOKEN_KEY: 'bookshelf_access_token',
  api: { post: jest.fn() },
}));
jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureMessage: jest.fn() },
}));
const mockStore = new Map<string, string>();
jest.mock('../../lib/storage', () => ({
  getItem: jest.fn(async (k: string) => mockStore.get(k) ?? null),
  setItem: jest.fn(async (k: string, v: string) => void mockStore.set(k, v)),
  deleteItem: jest.fn(async (k: string) => void mockStore.delete(k)),
}));

const post = api.post as jest.Mock;
const capture = Sentry.captureMessage as jest.Mock;
const breadcrumb = Sentry.addBreadcrumb as jest.Mock;

const NONCE_1 = 'cGF5bG9hZDE.c2lnMQ';
const NONCE_2 = 'cGF5bG9hZDI.c2lnMg';
const KEY_ID = 'a2V5LWlkLXNlY3JldA==';
const ATTESTATION = 'YXR0ZXN0YXRpb24tc2VjcmV0';
const ASSERTION = 'YXNzZXJ0aW9uLXNlY3JldA==';
const TOKEN = 'play-integrity-token-secret';
const PROJECT = '123456789012';
const USER_A = '0b0e8c3e-1111-4a6b-9c1d-aaaaaaaaaaaa';
const USER_B = '0b0e8c3e-2222-4a6b-9c1d-bbbbbbbbbbbb';
/** Account A's key slot (most tests run signed in as A). */
const KEY_SLOT = appAttestKeyStorageKey(USER_A);

/** Sign in as `user`: an access token whose payload carries `sub`. */
function signIn(user: string | null) {
  if (!user) {
    mockStore.delete('bookshelf_access_token');
    return;
  }
  const payload = Buffer.from(JSON.stringify({ sub: user, type: 'access' })).toString('base64url');
  mockStore.set('bookshelf_access_token', `eyJhbGciOiJSUzI1NiJ9.${payload}.sig`);
}

const http = (status: number, detail: string) => ({ response: { status, data: { detail } } });

/** Answer POST /attestation/nonce with the given nonces in turn, /register with 200. */
function serveNonces(...nonces: string[]) {
  const queue = [...nonces];
  post.mockImplementation(async (path: string) => {
    if (path === '/attestation/nonce') return { data: { nonce: queue.shift() } };
    if (path === '/attestation/register') return { data: { registered: true } };
    throw new Error(`unexpected ${path}`);
  });
}

let os: string = 'ios';
const originalDev = (globalThis as { __DEV__?: boolean }).__DEV__;
const originalOS = Platform.OS;

beforeAll(() => {
  Object.defineProperty(Platform, 'OS', { configurable: true, get: () => os });
});
afterAll(() => {
  Object.defineProperty(Platform, 'OS', { configurable: true, value: originalOS });
  (globalThis as { __DEV__?: boolean }).__DEV__ = originalDev;
});

beforeEach(() => {
  jest.clearAllMocks();
  resetAttestationState();
  mockStore.clear();
  signIn(USER_A);
  os = 'ios';
  (globalThis as { __DEV__?: boolean }).__DEV__ = false;
  mockIntegrity.supported = true;
  mockIntegrity.generateKeyAsync.mockResolvedValue(KEY_ID);
  mockIntegrity.attestKeyAsync.mockResolvedValue(ATTESTATION);
  mockIntegrity.generateAssertionAsync.mockResolvedValue(ASSERTION);
  mockIntegrity.prepareIntegrityTokenProviderAsync.mockResolvedValue(undefined);
  mockIntegrity.requestIntegrityCheckAsync.mockResolvedValue(TOKEN);
  process.env.EXPO_PUBLIC_PLAY_INTEGRITY_CLOUD_PROJECT_NUMBER = PROJECT;
});
afterEach(() => {
  jest.useRealTimers();
  delete process.env.EXPO_PUBLIC_PLAY_INTEGRITY_CLOUD_PROJECT_NUMBER;
});

function capturedTags(n = 0) {
  return capture.mock.calls[n][1].tags as Record<string, string>;
}

describe('iOS App Attest', () => {
  it('first run: nonce, generate + attest key, register, store the key id, then assert with a fresh nonce', async () => {
    serveNonces(NONCE_1, NONCE_2);
    const fields = await getAttestationFields();

    expect(mockIntegrity.attestKeyAsync).toHaveBeenCalledWith(KEY_ID, NONCE_1);
    expect(post.mock.calls.map((c) => c[0])).toEqual([
      '/attestation/nonce',
      '/attestation/register',
      '/attestation/nonce',
    ]);
    // The body the server's RegisterRequest reads.
    expect(post.mock.calls[1][1]).toEqual({
      key_id: KEY_ID,
      attestation_b64: ATTESTATION,
      nonce: NONCE_1,
    });
    expect(storage.setItem).toHaveBeenCalledWith(KEY_SLOT, KEY_ID);
    expect(mockIntegrity.generateAssertionAsync).toHaveBeenCalledWith(KEY_ID, NONCE_2);
    expect(fields).toEqual({
      platform: 'ios',
      nonce: NONCE_2,
      keyId: KEY_ID,
      assertion: ASSERTION,
    });
    expect(capture).not.toHaveBeenCalled();
  });

  it('repeat: uses the stored key, one nonce per call, no registration', async () => {
    mockStore.set(KEY_SLOT, KEY_ID);
    serveNonces(NONCE_1, NONCE_2);
    const first = await getAttestationFields();
    const second = await getAttestationFields();

    expect(mockIntegrity.generateKeyAsync).not.toHaveBeenCalled();
    expect(post.mock.calls.map((c) => c[0])).toEqual(['/attestation/nonce', '/attestation/nonce']);
    expect(first?.nonce).toBe(NONCE_1);
    expect(second?.nonce).toBe(NONCE_2);
  });

  it('an invalid key clears the stored id, so the next scan registers again', async () => {
    mockStore.set(KEY_SLOT, 'stale-key');
    serveNonces(NONCE_1, NONCE_2, 'bm9uY2Uz.c2lnMw');
    mockIntegrity.generateAssertionAsync.mockRejectedValueOnce({
      code: 'ERR_APP_INTEGRITY_INVALID_KEY',
    });

    expect(await getAttestationFields()).toBeNull();
    expect(storage.deleteItem).toHaveBeenCalledWith(KEY_SLOT);
    expect(capturedTags()).toEqual(
      expect.objectContaining({
        feature: 'scan',
        error: 'attestation_failure',
        'attestation.reason': 'key_invalid',
      })
    );

    const next = await getAttestationFields();
    expect(mockIntegrity.generateKeyAsync).toHaveBeenCalledTimes(1);
    expect(next?.keyId).toBe(KEY_ID);
  });

  it('unsupported (simulator, no App Attest): no calls, a breadcrumb but no Sentry event, then off for the session', async () => {
    mockIntegrity.supported = false;
    expect(await getAttestationFields()).toBeNull();
    expect(await getAttestationFields()).toBeNull();
    expect(post).not.toHaveBeenCalled();
    expect(capture).not.toHaveBeenCalled();
    expect(breadcrumb).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'attestation unsupported', data: { platform: 'ios' } })
    );
  });

  it('attestationActive: on for a release build until attestation is off for the session', async () => {
    expect(attestationActive()).toBe(true);
    mockIntegrity.supported = false;
    await getAttestationFields();
    expect(attestationActive()).toBe(false);
  });

  it('a registration the server rejects stores nothing and stops for the session', async () => {
    post.mockImplementation(async (path: string) => {
      if (path === '/attestation/nonce') return { data: { nonce: NONCE_1 } };
      throw http(400, 'attestation_invalid');
    });
    expect(await getAttestationFields()).toBeNull();
    expect(storage.setItem).not.toHaveBeenCalled();
    expect(capturedTags()['attestation.reason']).toBe('register_rejected');

    post.mockClear();
    expect(await getAttestationFields()).toBeNull();
    expect(post).not.toHaveBeenCalled();
  });

  it('a server without attestation set up (503) stops for the session without a Sentry event', async () => {
    post.mockRejectedValue(http(503, 'attestation_not_configured'));
    expect(await getAttestationFields()).toBeNull();
    expect(await getAttestationFields()).toBeNull();
    expect(post).toHaveBeenCalledTimes(1);
    expect(capture).not.toHaveBeenCalled();
    expect(breadcrumb).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'attestation not configured' })
    );
  });

  it('a network failure on the nonce falls back and is retried on the next scan', async () => {
    post.mockRejectedValueOnce(new Error('Network Error'));
    expect(await getAttestationFields()).toBeNull();
    expect(capturedTags()).toEqual(
      expect.objectContaining({
        'attestation.reason': 'nonce_failed',
        'attestation.code': 'network',
      })
    );
    mockStore.set(KEY_SLOT, KEY_ID);
    serveNonces(NONCE_2);
    expect((await getAttestationFields())?.nonce).toBe(NONCE_2);
  });

  it('a malformed nonce is never signed', async () => {
    mockStore.set(KEY_SLOT, KEY_ID);
    serveNonces('not a nonce');
    expect(await getAttestationFields()).toBeNull();
    expect(mockIntegrity.generateAssertionAsync).not.toHaveBeenCalled();
    expect(capturedTags()['attestation.code']).toBe('malformed');
  });

  it('a server rejection clears the stored key and is logged', async () => {
    mockStore.set(KEY_SLOT, KEY_ID);
    await onAttestationRejected({ platform: 'ios', nonce: NONCE_1, keyId: KEY_ID });
    expect(mockStore.has(KEY_SLOT)).toBe(false);
    expect(capturedTags()['attestation.reason']).toBe('server_rejected');
  });

  it.each([
    [
      'SecureStore cannot be read',
      () =>
        (storage.getItem as jest.Mock)
          .mockImplementationOnce(async (k: string) => mockStore.get(k) ?? null) // the account
          .mockRejectedValueOnce(new Error('keychain')), // the key slot
      'storage_failed',
    ],
    [
      'Apple cannot attest the key',
      () =>
        mockIntegrity.attestKeyAsync.mockRejectedValueOnce({
          code: 'ERR_APP_INTEGRITY_SERVER_UNAVAILABLE',
        }),
      'native_error',
    ],
    [
      'the key id cannot be stored',
      () => (storage.setItem as jest.Mock).mockRejectedValueOnce(new Error('keychain')),
      'storage_failed',
    ],
  ])('%s: falls back and is logged', async (_label, fail, reason) => {
    serveNonces(NONCE_1, NONCE_2);
    fail();
    expect(await getAttestationFields()).toBeNull();
    expect(capturedTags()['attestation.reason']).toBe(reason);
  });

  it('concurrent first runs share one registration', async () => {
    serveNonces(NONCE_1, NONCE_2, 'bm9uY2Uz.c2lnMw');
    await Promise.all([getAttestationFields(), getAttestationFields()]);
    expect(mockIntegrity.generateKeyAsync).toHaveBeenCalledTimes(1);
  });
});

describe('accounts on one device', () => {
  const KEY_B = 'a2V5LWItc2VjcmV0';

  it('each account has its own key: B registers afresh, A keeps its key after B', async () => {
    mockStore.set(KEY_SLOT, KEY_ID);
    serveNonces(NONCE_1, NONCE_2, 'bm9uY2Uz.c2lnMw', 'bm9uY2U0.c2lnNA');
    expect((await getAttestationFields())?.keyId).toBe(KEY_ID);

    resetAttestationState(); // sign-out
    signIn(USER_B);
    mockIntegrity.generateKeyAsync.mockResolvedValueOnce(KEY_B);
    const b = await getAttestationFields();
    expect(b?.keyId).toBe(KEY_B);
    expect(mockStore.get(appAttestKeyStorageKey(USER_B))).toBe(KEY_B);

    signIn(USER_A);
    expect((await getAttestationFields())?.keyId).toBe(KEY_ID);
    expect(mockIntegrity.generateKeyAsync).toHaveBeenCalledTimes(1);
  });

  it('a change of account without a reset is noticed too', async () => {
    mockStore.set(KEY_SLOT, KEY_ID);
    serveNonces(NONCE_1, NONCE_2, 'bm9uY2Uz.c2lnMw');
    await getAttestationFields();
    signIn(USER_B);
    mockIntegrity.generateKeyAsync.mockResolvedValueOnce(KEY_B);
    expect((await getAttestationFields())?.keyId).toBe(KEY_B);
  });

  it('the session latch belongs to the account: B attests after A was latched off', async () => {
    post.mockImplementation(async (path: string) => {
      if (path === '/attestation/nonce') return { data: { nonce: NONCE_1 } };
      throw http(400, 'attestation_invalid');
    });
    expect(await getAttestationFields()).toBeNull();
    expect(attestationActive()).toBe(false);

    resetAttestationState();
    expect(attestationActive()).toBe(true);
    signIn(USER_B);
    mockStore.set(appAttestKeyStorageKey(USER_B), KEY_B);
    serveNonces(NONCE_2);
    expect((await getAttestationFields())?.keyId).toBe(KEY_B);
  });

  it('a registration still running at sign-out is aborted and never stored for anyone', async () => {
    jest.useFakeTimers();
    let finishRegister: () => void = () => {};
    post.mockImplementation((path: string) => {
      if (path === '/attestation/nonce') return Promise.resolve({ data: { nonce: NONCE_1 } });
      return new Promise((resolve) => {
        finishRegister = () => resolve({ data: { registered: true } });
      });
    });
    const pending = getAttestationFields();
    await jest.advanceTimersByTimeAsync(10);
    resetAttestationState(); // sign-out
    signIn(USER_B);
    finishRegister();
    await jest.advanceTimersByTimeAsync(ATTESTATION_TIMEOUT_MS);
    expect(await pending).toBeNull();
    expect(storage.setItem).not.toHaveBeenCalled();
    expect(mockStore.has(appAttestKeyStorageKey(USER_B))).toBe(false);
  });

  it('signed out: nothing is attested', async () => {
    signIn(null);
    expect(await getAttestationFields()).toBeNull();
    expect(post).not.toHaveBeenCalled();
  });

  it('one scan aborting does not cancel the registration another scan waits for', async () => {
    let finishRegister: () => void = () => {};
    const nonces = [NONCE_1, NONCE_2];
    post.mockImplementation((path: string) => {
      if (path === '/attestation/nonce')
        return Promise.resolve({ data: { nonce: nonces.shift() } });
      return new Promise((resolve) => {
        finishRegister = () => resolve({ data: { registered: true } });
      });
    });
    const first = new AbortController();
    const one = getAttestationFields(first.signal);
    const two = getAttestationFields();
    await new Promise((r) => setTimeout(r, 0));
    first.abort();
    expect(await one).toBeNull();
    finishRegister();
    expect((await two)?.keyId).toBe(KEY_ID);
    expect(mockIntegrity.generateKeyAsync).toHaveBeenCalledTimes(1);
    expect(capture).not.toHaveBeenCalled();
  });

  it('reads the account only from a well-formed token', () => {
    signIn(USER_A);
    expect(userIdFromAccessToken(mockStore.get('bookshelf_access_token'))).toBe(USER_A);
    expect(userIdFromAccessToken('not-a-jwt')).toBeNull();
    expect(userIdFromAccessToken('a.!!!.c')).toBeNull();
    const notUuid = Buffer.from(JSON.stringify({ sub: '../x' })).toString('base64url');
    expect(userIdFromAccessToken(`h.${notUuid}.s`)).toBeNull();
    expect(userIdFromAccessToken(null)).toBeNull();
  });
});

describe('Android Play Integrity', () => {
  beforeEach(() => {
    os = 'android';
  });

  it('prepares the provider once and requests a token with each fresh nonce', async () => {
    serveNonces(NONCE_1, NONCE_2);
    const first = await getAttestationFields();
    const second = await getAttestationFields();

    expect(mockIntegrity.prepareIntegrityTokenProviderAsync).toHaveBeenCalledTimes(1);
    expect(mockIntegrity.prepareIntegrityTokenProviderAsync).toHaveBeenCalledWith(PROJECT);
    expect(mockIntegrity.requestIntegrityCheckAsync.mock.calls).toEqual([[NONCE_1], [NONCE_2]]);
    expect(first).toEqual({ platform: 'android', nonce: NONCE_1, token: TOKEN });
    expect(second?.nonce).toBe(NONCE_2);
    expect(mockIntegrity.generateKeyAsync).not.toHaveBeenCalled();
  });

  it('no cloud project number: nothing is requested and nothing is reported', async () => {
    delete process.env.EXPO_PUBLIC_PLAY_INTEGRITY_CLOUD_PROJECT_NUMBER;
    expect(await getAttestationFields()).toBeNull();
    expect(post).not.toHaveBeenCalled();
    expect(mockIntegrity.prepareIntegrityTokenProviderAsync).not.toHaveBeenCalled();
    expect(capture).not.toHaveBeenCalled();
  });

  it('no Play Store / Play services: a breadcrumb but no Sentry event, then off for the session', async () => {
    mockIntegrity.prepareIntegrityTokenProviderAsync.mockRejectedValue({
      code: 'ERR_APP_INTEGRITY_API_NOT_AVAILABLE',
    });
    expect(await getAttestationFields()).toBeNull();
    expect(await getAttestationFields()).toBeNull();
    expect(mockIntegrity.prepareIntegrityTokenProviderAsync).toHaveBeenCalledTimes(1);
    expect(capture).not.toHaveBeenCalled();
    expect(breadcrumb).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'attestation unsupported', data: { platform: 'android' } })
    );
  });

  it('a real Play Integrity failure is logged as play_integrity_unavailable', async () => {
    serveNonces(NONCE_1);
    mockIntegrity.requestIntegrityCheckAsync.mockRejectedValueOnce({
      code: 'ERR_APP_INTEGRITY_NETWORK_ERROR',
    });
    expect(await getAttestationFields()).toBeNull();
    expect(capturedTags()).toEqual(
      expect.objectContaining({
        feature: 'scan',
        error: 'play_integrity_unavailable',
        'attestation.reason': 'native_error',
        'attestation.code': 'ERR_APP_INTEGRITY_NETWORK_ERROR',
      })
    );
  });

  it('a server rejection turns Android attestation off for the session (no double posts)', async () => {
    serveNonces(NONCE_1, NONCE_2);
    const fields = await getAttestationFields();
    await onAttestationRejected(fields!);
    expect(capturedTags()['attestation.reason']).toBe('server_rejected');
    expect(attestationActive()).toBe(false);

    post.mockClear();
    mockIntegrity.requestIntegrityCheckAsync.mockClear();
    expect(await getAttestationFields()).toBeNull();
    expect(post).not.toHaveBeenCalled();
    expect(mockIntegrity.requestIntegrityCheckAsync).not.toHaveBeenCalled();
  });

  it('a timed-out scan does not go on to spend a Play Integrity request', async () => {
    jest.useFakeTimers();
    let answerNonce: () => void = () => {};
    post.mockImplementation(
      () =>
        new Promise((resolve) => {
          answerNonce = () => resolve({ data: { nonce: NONCE_1 } });
        })
    );
    const pending = getAttestationFields();
    await jest.advanceTimersByTimeAsync(ATTESTATION_TIMEOUT_MS);
    expect(await pending).toBeNull();
    // The nonce request was aborted with the work; a late answer goes nowhere.
    expect(post.mock.calls[0][2].signal.aborted).toBe(true);
    answerNonce();
    await jest.advanceTimersByTimeAsync(0);
    expect(mockIntegrity.requestIntegrityCheckAsync).not.toHaveBeenCalled();
  });

  it('an invalid provider is prepared again on the next scan', async () => {
    serveNonces(NONCE_1, NONCE_2);
    mockIntegrity.requestIntegrityCheckAsync.mockRejectedValueOnce({
      code: 'ERR_APP_INTEGRITY_PROVIDER_INVALID',
    });
    expect(await getAttestationFields()).toBeNull();
    expect((await getAttestationFields())?.token).toBe(TOKEN);
    expect(mockIntegrity.prepareIntegrityTokenProviderAsync).toHaveBeenCalledTimes(2);
  });

  it('a failed prepare is retried on the next scan', async () => {
    serveNonces(NONCE_1);
    mockIntegrity.prepareIntegrityTokenProviderAsync.mockRejectedValueOnce({
      code: 'ERR_APP_INTEGRITY_NETWORK_ERROR',
    });
    expect(await getAttestationFields()).toBeNull();
    expect((await getAttestationFields())?.token).toBe(TOKEN);
  });
});

describe('when attestation is skipped', () => {
  it.each([
    ['a dev build (simulator / emulator from Metro)', 'ios', true],
    ['a dev Android build', 'android', true],
    ['web', 'web', false],
  ])('%s sends nothing and calls nothing', async (_label, platform, dev) => {
    os = platform;
    (globalThis as { __DEV__?: boolean }).__DEV__ = dev;
    expect(attestationActive()).toBe(false);
    expect(await getAttestationFields()).toBeNull();
    expect(post).not.toHaveBeenCalled();
    expect(mockIntegrity.generateKeyAsync).not.toHaveBeenCalled();
    expect(mockIntegrity.prepareIntegrityTokenProviderAsync).not.toHaveBeenCalled();
    expect(capture).not.toHaveBeenCalled();
  });

  it('an aborted scan stops without logging', async () => {
    const controller = new AbortController();
    controller.abort();
    expect(await getAttestationFields(controller.signal)).toBeNull();
    expect(post).not.toHaveBeenCalled();
    expect(capture).not.toHaveBeenCalled();
  });

  it('a scan aborted while attesting stops at once without logging', async () => {
    mockStore.set(KEY_SLOT, KEY_ID);
    post.mockImplementation(() => new Promise(() => {}));
    const controller = new AbortController();
    const pending = getAttestationFields(controller.signal);
    controller.abort();
    expect(await pending).toBeNull();
    expect(capture).not.toHaveBeenCalled();
  });
});

describe('timeout', () => {
  it('gives up after 5 s and sends the scan without attestation', async () => {
    jest.useFakeTimers();
    mockStore.set(KEY_SLOT, KEY_ID);
    post.mockImplementation(() => new Promise(() => {}));
    const pending = getAttestationFields();
    await jest.advanceTimersByTimeAsync(ATTESTATION_TIMEOUT_MS - 1);
    let settled = false;
    void pending.then(() => (settled = true));
    await Promise.resolve();
    expect(settled).toBe(false);
    await jest.advanceTimersByTimeAsync(1);
    expect(await pending).toBeNull();
    expect(capturedTags()).toEqual(
      expect.objectContaining({ error: 'attestation_failure', 'attestation.reason': 'timeout' })
    );
  });

  it('a registration that finishes after the timeout is kept for the next scan', async () => {
    jest.useFakeTimers();
    let finishRegister: () => void = () => {};
    post.mockImplementation((path: string) => {
      if (path === '/attestation/nonce') return Promise.resolve({ data: { nonce: NONCE_1 } });
      return new Promise((resolve) => {
        finishRegister = () => resolve({ data: { registered: true } });
      });
    });
    const pending = getAttestationFields();
    await jest.advanceTimersByTimeAsync(ATTESTATION_TIMEOUT_MS);
    expect(await pending).toBeNull();
    finishRegister();
    await jest.advanceTimersByTimeAsync(0);
    expect(mockStore.get(KEY_SLOT)).toBe(KEY_ID);
  });
});

describe('nothing secret leaves the device in diagnostics', () => {
  it('breadcrumbs and Sentry events carry no nonce, key id, attestation, assertion or token', async () => {
    serveNonces(NONCE_1, NONCE_2);
    const ios = await getAttestationFields();
    const form = new FormData();
    appendAttestationFields(form, ios);
    mockIntegrity.generateAssertionAsync.mockRejectedValueOnce({
      code: 'ERR_APP_INTEGRITY_UNKNOWN',
    });
    serveNonces('bm9uY2Uz.c2lnMw');
    await getAttestationFields();
    await onAttestationRejected(ios!);

    os = 'android';
    serveNonces('bm9uY2U0.c2lnNA');
    appendAttestationFields(new FormData(), await getAttestationFields());

    expect(breadcrumb.mock.calls.map((c) => c[0].message)).toEqual(
      expect.arrayContaining([
        'nonce requested',
        'key registration requested',
        'key registered',
        'assertion generated',
        'scan submitted with attestation',
        'integrity token received',
      ])
    );
    const logged = JSON.stringify([breadcrumb.mock.calls, capture.mock.calls]);
    for (const secret of [NONCE_1, NONCE_2, KEY_ID, ATTESTATION, ASSERTION, TOKEN, 'bm9uY2U']) {
      expect(logged).not.toContain(secret);
    }
  });

  it('the Sentry scrubber redacts attestation fields if they ever reach a breadcrumb', () => {
    const scrubbed = scrubBreadcrumb({
      data: {
        nonce: NONCE_1,
        'attestation-token': TOKEN,
        attestation_b64: ATTESTATION,
        key_id: KEY_ID,
        keyId: KEY_ID,
        'attestation-key-id': KEY_ID,
      },
    }) as { data: Record<string, string> };
    expect(JSON.stringify(scrubbed)).not.toMatch(/c2lnMQ|play-integrity|YXR0ZXN0|a2V5LWlk/);
  });
});

describe('form fields and server answers', () => {
  function parts(form: FormData) {
    const rn = (form as FormData & { _parts?: [string, unknown][] })._parts;
    return Object.fromEntries(rn ?? Array.from(form.entries()));
  }

  it('iOS adds platform, nonce, key id and assertion under the server names', () => {
    const form = new FormData();
    appendAttestationFields(form, {
      platform: 'ios',
      nonce: NONCE_1,
      keyId: KEY_ID,
      assertion: ASSERTION,
    });
    expect(parts(form)).toEqual({
      'attestation-platform': 'ios',
      'attestation-nonce': NONCE_1,
      'attestation-key-id': KEY_ID,
      'attestation-assertion': ASSERTION,
    });
  });

  it('Android adds platform, nonce and token', () => {
    const form = new FormData();
    appendAttestationFields(form, { platform: 'android', nonce: NONCE_1, token: TOKEN });
    expect(parts(form)).toEqual({
      'attestation-platform': 'android',
      'attestation-nonce': NONCE_1,
      'attestation-token': TOKEN,
    });
  });

  it('null adds nothing', () => {
    const form = new FormData();
    appendAttestationFields(form, null);
    expect(parts(form)).toEqual({});
    expect(breadcrumb).not.toHaveBeenCalled();
  });

  it('recognises only the attestation 403', () => {
    expect(isAttestationRejection(http(403, 'Attestation verification failed'))).toBe(true);
    expect(isAttestationRejection(http(403, 'Turnstile verification failed'))).toBe(false);
    expect(isAttestationRejection(new Error('x'))).toBe(false);
  });

  it('reads only a numeric cloud project number', () => {
    process.env.EXPO_PUBLIC_PLAY_INTEGRITY_CLOUD_PROJECT_NUMBER = ' 42 ';
    expect(playIntegrityCloudProjectNumber()).toBe('42');
    process.env.EXPO_PUBLIC_PLAY_INTEGRITY_CLOUD_PROJECT_NUMBER = 'my-project';
    expect(playIntegrityCloudProjectNumber()).toBeNull();
  });
});
