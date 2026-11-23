/**
 * executeScan and device attestation (#870): every POST /scan gets its own
 * attestation (fresh nonce) after the AI consent check, a failure sends the
 * scan without it, and a server rejection re-sends once without it.
 */
import { api } from '../../lib/api';
import type { AttestationFields } from '../../lib/attestation/attestation';
import { getAttestationFields, onAttestationRejected } from '../../lib/attestation/attestation';
import { executeScan } from '../../lib/scan/executeScan';
import { makeImageJob, makeScanExecDeps } from '../helpers/scanExecDeps';

jest.mock('../../lib/api', () => ({ api: { get: jest.fn(), post: jest.fn() } }));
jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureMessage: jest.fn(), captureException: jest.fn() },
}));
jest.mock('../../lib/attestation/attestation', () => {
  const actual = jest.requireActual('../../lib/attestation/attestation');
  return {
    ...actual,
    getAttestationFields: jest.fn(),
    onAttestationRejected: jest.fn(async () => {}),
  };
});

const post = api.post as jest.Mock;
const attest = getAttestationFields as jest.Mock;
const rejected = onAttestationRejected as jest.Mock;
const okResponse = { data: { books: [{ title: 'T', author: 'A' }], no_match: null } };
const REJECTED = { response: { status: 403, data: { detail: 'Attestation verification failed' } } };

function iosFields(n: number): AttestationFields {
  return { platform: 'ios', nonce: `bm9uY2U${n}.c2ln`, keyId: 'key-1', assertion: `assert-${n}` };
}

function sentFields(call: number): Record<string, unknown> {
  const form = post.mock.calls[call][1] as FormData;
  return Object.fromEntries(
    Array.from(form.entries()).filter(([name]) => name.startsWith('attestation-'))
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  post.mockReset();
});

it('adds the attestation fields to the POST /scan form on success', async () => {
  attest.mockResolvedValueOnce(iosFields(1));
  post.mockResolvedValueOnce(okResponse);
  const deps = makeScanExecDeps();
  await executeScan(deps, makeImageJob());

  expect(sentFields(0)).toEqual({
    'attestation-platform': 'ios',
    'attestation-nonce': 'bm9uY2U1.c2ln',
    'attestation-key-id': 'key-1',
    'attestation-assertion': 'assert-1',
  });
  expect(deps.updateJob).toHaveBeenCalledWith(
    'job-1',
    expect.objectContaining({ status: 'complete' })
  );
});

it('gets a fresh attestation (new nonce) for every post, scan_in_progress retries included', async () => {
  jest.useFakeTimers();
  let n = 0;
  attest.mockImplementation(async () => iosFields(++n));
  post
    .mockRejectedValueOnce({
      response: {
        status: 429,
        data: { detail: 'scan_in_progress' },
        headers: { 'retry-after': '1' },
      },
    })
    .mockResolvedValueOnce(okResponse);
  const done = executeScan(makeScanExecDeps(), makeImageJob());
  await jest.advanceTimersByTimeAsync(5_000);
  await done;
  jest.useRealTimers();

  expect(attest).toHaveBeenCalledTimes(2);
  expect(sentFields(0)['attestation-nonce']).toBe('bm9uY2U1.c2ln');
  expect(sentFields(1)['attestation-nonce']).toBe('bm9uY2U2.c2ln');
});

it('sends the scan without attestation fields when attestation is unavailable', async () => {
  attest.mockResolvedValueOnce(null);
  post.mockResolvedValueOnce(okResponse);
  const deps = makeScanExecDeps();
  await executeScan(deps, makeImageJob());

  expect(post).toHaveBeenCalledTimes(1);
  expect(sentFields(0)).toEqual({});
  expect((post.mock.calls[0][1] as FormData).get('attempt')).toBe('1');
  expect(deps.updateJob).toHaveBeenCalledWith(
    'job-1',
    expect.objectContaining({ status: 'complete' })
  );
});

it('checks AI consent before any attestation call; without consent nothing is attested', async () => {
  const order: string[] = [];
  const check = jest.fn(async () => {
    order.push('consent');
    return true;
  });
  attest.mockImplementation(async () => {
    order.push('attest');
    return iosFields(1);
  });
  post.mockResolvedValueOnce(okResponse);
  await executeScan(
    makeScanExecDeps({
      aiConsentRef: { current: { check, ensure: check, markRequired: jest.fn() } },
    }),
    makeImageJob()
  );
  expect(order).toEqual(['consent', 'consent', 'attest']);

  attest.mockClear();
  post.mockClear();
  const refuse = jest.fn(async () => false);
  await executeScan(
    makeScanExecDeps({
      aiConsentRef: { current: { check: refuse, ensure: refuse, markRequired: jest.fn() } },
    }),
    makeImageJob()
  );
  expect(attest).not.toHaveBeenCalled();
  expect(post).not.toHaveBeenCalled();
});

it('a 403 attestation rejection forgets the attestation and re-sends once without it', async () => {
  attest.mockResolvedValueOnce(iosFields(1));
  post.mockRejectedValueOnce(REJECTED).mockResolvedValueOnce(okResponse);
  let token = 0;
  const getToken = jest.fn(async () => `tok-${++token}`);
  const deps = makeScanExecDeps({ getTurnstileTokenRef: { current: getToken } });
  await executeScan(deps, makeImageJob());

  expect(rejected).toHaveBeenCalledWith(iosFields(1));
  expect(attest).toHaveBeenCalledTimes(1);
  expect(post).toHaveBeenCalledTimes(2);
  expect(sentFields(1)).toEqual({});
  // The re-send carries its own Turnstile token, so Turnstile decides.
  expect((post.mock.calls[1][1] as FormData).get('cf-turnstile-response')).toBe('tok-2');
  expect(deps.updateJob).toHaveBeenCalledWith(
    'job-1',
    expect.objectContaining({ status: 'complete' })
  );
});

it('a 403 attestation rejection for a scan sent without attestation is not re-sent', async () => {
  attest.mockResolvedValueOnce(null);
  post.mockRejectedValueOnce(REJECTED);
  const deps = makeScanExecDeps();
  await executeScan(deps, makeImageJob());

  expect(post).toHaveBeenCalledTimes(1);
  expect(rejected).not.toHaveBeenCalled();
  expect(deps.updateJob).toHaveBeenCalledWith(
    'job-1',
    expect.objectContaining({ status: 'failed' })
  );
});

it('the real module sends nothing new in a dev build (simulator / emulator)', async () => {
  const actual = jest.requireActual('../../lib/attestation/attestation');
  attest.mockImplementation(actual.getAttestationFields);
  post.mockResolvedValueOnce(okResponse);
  await executeScan(makeScanExecDeps(), makeImageJob());

  // Jest runs with __DEV__ true: no /attestation call, no attestation fields.
  expect(post.mock.calls.map((c) => c[0])).toEqual(['/scan']);
  expect(sentFields(0)).toEqual({});
});

it('text searches never attest', async () => {
  (api.get as jest.Mock).mockResolvedValueOnce({ data: [] });
  await executeScan(makeScanExecDeps(), makeImageJob({ type: 'text', query: 'dune' }));
  expect(attest).not.toHaveBeenCalled();
});
