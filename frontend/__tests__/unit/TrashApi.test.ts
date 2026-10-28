/**
 * #683: listTrash / restoreUserBook and the restore error mapping.
 */
jest.mock('../../lib/storage', () => ({
  getItem: jest.fn().mockResolvedValue('token'),
  setItem: jest.fn(),
  deleteItem: jest.fn(),
}));

jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: jest.fn(), captureException: jest.fn() },
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

import { api, listTrash, restoreUserBook } from '../../lib/api';
import { formatLocalDate, restoreFailure } from '../../lib/trash';

type Seen = { method?: string; url?: string };
let seen: Seen = {};

function respond(status: number, data: unknown) {
  api.defaults.adapter = async (config) => {
    seen = { method: config.method, url: config.url };
    const response = { status, data, headers: {}, config, statusText: '' };
    if (status >= 400) {
      throw Object.assign(new Error(`Request failed with status code ${status}`), {
        isAxiosError: true,
        config,
        response,
      });
    }
    return response;
  };
}

async function failureOf(status: number, detail?: unknown) {
  respond(status, detail === undefined ? {} : { detail });
  try {
    await restoreUserBook('ub-1');
  } catch (err) {
    return err;
  }
  throw new Error('expected restoreUserBook to reject');
}

beforeEach(() => {
  seen = {};
});

describe('listTrash', () => {
  it('GETs /user-books/trash and returns the entries', async () => {
    const rows = [
      {
        id: 'ub-1',
        status: 'read',
        book: { id: 'b-1', title: 'Dune', author: 'Frank Herbert' },
        deleted_at: '2026-10-01T10:00:00Z',
        purge_after: '2026-10-31T10:00:00Z',
      },
    ];
    respond(200, rows);
    await expect(listTrash()).resolves.toEqual(rows);
    expect(seen).toEqual({ method: 'get', url: '/user-books/trash' });
  });

  it('rejects on a server error', async () => {
    respond(500, {});
    await expect(listTrash()).rejects.toBeTruthy();
  });
});

describe('restoreUserBook', () => {
  it('POSTs /user-books/{id}/restore and returns the restored entry', async () => {
    respond(200, { id: 'ub-1', status: 'read' });
    await expect(restoreUserBook('ub-1')).resolves.toEqual({ id: 'ub-1', status: 'read' });
    expect(seen).toEqual({ method: 'post', url: '/user-books/ub-1/restore' });
  });

  it('maps 409 restore_conflict', async () => {
    expect(restoreFailure(await failureOf(409, 'restore_conflict'))).toBe('conflict');
  });

  it('maps 409 unverified_limit_reached', async () => {
    expect(restoreFailure(await failureOf(409, 'unverified_limit_reached'))).toBe('limit');
  });

  it('maps 404', async () => {
    expect(restoreFailure(await failureOf(404, 'Not found'))).toBe('not_found');
  });

  it('maps 403 forbidden to unavailable, and other failures to failed', async () => {
    expect(restoreFailure(await failureOf(403, 'forbidden'))).toBe('unavailable');
    expect(restoreFailure(await failureOf(500))).toBe('failed');
    expect(restoreFailure(await failureOf(409, 'something_else'))).toBe('failed');
    expect(restoreFailure({ message: 'Network Error' })).toBe('failed');
    expect(restoreFailure(undefined)).toBe('failed');
  });
});

describe('formatLocalDate', () => {
  it('formats a date and returns "" for an invalid one', () => {
    expect(formatLocalDate('2026-10-01T12:00:00Z', 'en')).toMatch(/2026/);
    expect(formatLocalDate('nope', 'en')).toBe('');
  });
});
