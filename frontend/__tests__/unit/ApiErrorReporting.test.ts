/**
 * #724: unexpected API failures are reported to Sentry once per session with
 * status/path/detail tags, and a 403 never signs the user out.
 */
import { api, setAuthFailureCallback } from '../../lib/api';
import {
  isServiceUnavailable,
  pathTemplate,
  resetApiErrorReporting,
} from '../../lib/apiErrorReporting';

const mockCapture = jest.fn();
const mockDeleteItem = jest.fn();

jest.mock('../../lib/storage', () => ({
  getItem: jest.fn().mockResolvedValue('SECRET-ACCESS-TOKEN'),
  setItem: jest.fn(),
  deleteItem: (...args: unknown[]) => mockDeleteItem(...args),
}));

jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureException: (...a: unknown[]) => mockCapture(...a) },
  initSentry: jest.fn(),
}));

jest.mock('axios', () => {
  const savedRS = globalThis.ReadableStream;
  // @ts-expect-error avoid axios fetch adapter probe conflict
  delete globalThis.ReadableStream;
  const actual = jest.requireActual('axios');
  globalThis.ReadableStream = savedRS;
  return actual;
});

function respondWith(status: number, detail?: unknown) {
  api.defaults.adapter = async (config) => {
    const response = { status, data: detail === undefined ? {} : { detail }, headers: {}, config };
    throw Object.assign(new Error(`Request failed with status code ${status}`), {
      isAxiosError: true,
      config,
      response,
    });
  };
}

const authFailure = jest.fn();

beforeEach(() => {
  jest.clearAllMocks();
  resetApiErrorReporting();
  setAuthFailureCallback(authFailure);
});

describe('API error reporting (#724)', () => {
  it('captures a 403 forbidden once, with status/path/detail tags', async () => {
    respondWith(403, 'forbidden');
    await expect(api.get('/user-books', { params: { status: 'reading' } })).rejects.toBeTruthy();
    expect(mockCapture).toHaveBeenCalledTimes(1);
    const [, ctx] = mockCapture.mock.calls[0];
    expect(ctx.tags).toMatchObject({
      'http.status': '403',
      'api.path': '/user-books',
      'api.detail': 'forbidden',
    });
  });

  it('does not capture the same failure twice in a session', async () => {
    respondWith(403, 'forbidden');
    await expect(api.get('/user-books')).rejects.toBeTruthy();
    await expect(api.get('/user-books?status=reading')).rejects.toBeTruthy();
    expect(mockCapture).toHaveBeenCalledTimes(1);
  });

  it('templates IDs out of the path', async () => {
    expect(pathTemplate('/user-books/3f2b8c1e-1a2b-4c3d-8e4f-0123456789ab?x=1')).toBe(
      '/user-books/:id'
    );
    expect(pathTemplate('https://api.example.com/books/42/cover')).toBe('/books/:id/cover');
    respondWith(403, 'forbidden');
    await expect(api.get('/user-books/3f2b8c1e-1a2b-4c3d-8e4f-0123456789ab')).rejects.toBeTruthy();
    expect(mockCapture.mock.calls[0][1].tags['api.path']).toBe('/user-books/:id');
  });

  it('templates ISBN-like, hash and long-slug segments, and keeps static route words', () => {
    expect(pathTemplate('/books/isbn/9780441013593')).toBe('/books/isbn/:id');
    expect(pathTemplate('/books/isbn/044101359X1')).toBe('/books/isbn/:id');
    expect(pathTemplate('/catalog/deadbeefdeadbeefdeadbeefdeadbeef')).toBe('/catalog/:id');
    expect(pathTemplate('/catalog/e3b0c44298fc1c149afbf4c8996fb924')).toBe('/catalog/:id');
    expect(pathTemplate('/books/the-hitchhikers-guide-to-the-galaxy-by-douglas')).toBe(
      '/books/:id'
    );
    for (const p of [
      '/user-books',
      '/scan',
      '/scan/usage',
      '/wishlist',
      '/books/search',
      '/auth/refresh',
      '/auth/me',
      '/user-books/unverified',
    ]) {
      expect(pathTemplate(p)).toBe(p);
    }
  });

  it.each([
    [403, 'enhanced_requires_premium'],
    [402, 'whatever'],
    [409, 'unverified_duplicate'],
    [429, 'daily_scan_limit'],
    [503, 'scan_unavailable'],
  ])('does not capture handled %s %s', async (status, detail) => {
    respondWith(status, detail);
    await expect(api.post('/user-books', {})).rejects.toBeTruthy();
    expect(mockCapture).not.toHaveBeenCalled();
  });

  it('does not capture network errors (no response)', async () => {
    api.defaults.adapter = async () => {
      throw Object.assign(new Error('Network Error'), { isAxiosError: true });
    };
    await expect(api.get('/user-books')).rejects.toBeTruthy();
    expect(mockCapture).not.toHaveBeenCalled();
  });

  it('captures POST /scan like any other path, deduped', async () => {
    respondWith(500, 'x');
    await expect(api.post('/scan', {})).rejects.toBeTruthy();
    await expect(api.post('/scan', {})).rejects.toBeTruthy();
    expect(mockCapture).toHaveBeenCalledTimes(1);
    expect(mockCapture.mock.calls[0][1].tags['api.path']).toBe('/scan');
  });

  it('captures a 5xx with a known detail tagged with it, and an unlisted one without', async () => {
    respondWith(503, 'some_unlisted_code');
    await expect(api.get('/user-books')).rejects.toBeTruthy();
    expect(mockCapture.mock.calls[0][1].tags['api.detail']).toBeUndefined();
    respondWith(500, 'forbidden');
    await expect(api.get('/books/search')).rejects.toBeTruthy();
    expect(mockCapture.mock.calls[1][1].tags).toMatchObject({
      'http.status': '500',
      'api.detail': 'forbidden',
    });
  });

  it('a 403 with an unknown detail is captured without api.detail and is not service-unavailable', async () => {
    respondWith(403, 'Some other refusal');
    await expect(api.get('/user-books')).rejects.toMatchObject({ response: { status: 403 } });
    expect(mockCapture).toHaveBeenCalledTimes(1);
    expect(mockCapture.mock.calls[0][1].tags['api.detail']).toBeUndefined();
    expect(
      isServiceUnavailable({ response: { status: 403, data: { detail: 'Some other refusal' } } })
    ).toBe(false);
  });

  it('omits unknown detail text from tags', async () => {
    respondWith(500, 'Some free text with user@example.com');
    await expect(api.get('/user-books')).rejects.toBeTruthy();
    const tags = mockCapture.mock.calls[0][1].tags;
    expect(tags['api.detail']).toBeUndefined();
    expect(JSON.stringify(mockCapture.mock.calls[0])).not.toContain('user@example.com');
  });

  it('never sends tokens, headers or bodies', async () => {
    respondWith(403, 'forbidden');
    await expect(api.get('/user-books')).rejects.toBeTruthy();
    const serialized = JSON.stringify(mockCapture.mock.calls[0]);
    expect(serialized).not.toContain('SECRET-ACCESS-TOKEN');
    expect(serialized).not.toMatch(/authorization|bearer/i);
    expect(mockCapture.mock.calls[0][0]).toBeInstanceOf(Error);
    expect((mockCapture.mock.calls[0][0] as Error & { config?: unknown }).config).toBeUndefined();
  });

  it('a 403 forbidden never signs the user out', async () => {
    respondWith(403, 'forbidden');
    await expect(api.get('/user-books')).rejects.toBeTruthy();
    expect(mockDeleteItem).not.toHaveBeenCalled();
    expect(authFailure).not.toHaveBeenCalled();
  });

  it('isServiceUnavailable is true only for 403 forbidden', () => {
    const e = (status: number, detail: string) => ({ response: { status, data: { detail } } });
    expect(isServiceUnavailable(e(403, 'forbidden'))).toBe(true);
    expect(isServiceUnavailable(e(403, 'enhanced_requires_premium'))).toBe(false);
    expect(isServiceUnavailable(e(401, 'forbidden'))).toBe(false);
    expect(isServiceUnavailable(new Error('x'))).toBe(false);
  });
});
