/**
 * #683: listTrash / restoreUserBook and the restore error mapping.
 */
import { api, listTrash, restoreUserBook } from '../../lib/api';
import { daysUntilPurge, PURGE_SOON_DAYS, restoreFailure, tidyLabel } from '../../lib/trash';

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

describe('daysUntilPurge', () => {
  const realTz = process.env.TZ;
  // A zone with daylight saving; the day counts must not depend on 23 / 25 hour days.
  beforeAll(() => {
    process.env.TZ = 'America/New_York';
  });
  afterAll(() => {
    if (realTz === undefined) delete process.env.TZ;
    else process.env.TZ = realTz;
  });

  const local = (y: number, m: number, d: number, h = 12, min = 0) => new Date(y, m - 1, d, h, min);
  const days = (purge: Date, now: Date) => daysUntilPurge(purge.toISOString(), now);

  it('is 0 on the purge day itself, at any time of day', () => {
    expect(days(local(2026, 6, 10, 0, 5), local(2026, 6, 10, 23, 55))).toBe(0);
    expect(days(local(2026, 6, 10, 23, 55), local(2026, 6, 10, 0, 5))).toBe(0);
  });

  it('counts calendar days, so 23:59 to 00:01 the next day is 1', () => {
    expect(days(local(2026, 6, 11, 0, 1), local(2026, 6, 10, 23, 59))).toBe(1);
    expect(days(local(2026, 7, 10), local(2026, 6, 10))).toBe(30);
  });

  it('stays right when the clocks go back (a 25-hour day)', () => {
    // 2026-11-01 is 25 hours long in New York: 26 hours apart, 2 calendar days.
    expect(days(local(2026, 11, 2, 0, 30), local(2026, 10, 31, 23, 30))).toBe(2);
    expect(days(local(2026, 11, 2), local(2026, 10, 31))).toBe(2);
  });

  it('stays right when the clocks go forward (a 23-hour day)', () => {
    // 2026-03-08 is 23 hours long: 47 hours apart, 2 calendar days.
    expect(days(local(2026, 3, 9), local(2026, 3, 7))).toBe(2);
    expect(days(local(2026, 3, 9, 0, 30), local(2026, 3, 7, 23, 30))).toBe(2);
  });

  it('reads a purge date already past as 0 and an unreadable one as null', () => {
    expect(days(local(2026, 6, 1), local(2026, 6, 10))).toBe(0);
    expect(daysUntilPurge('nope')).toBeNull();
  });

  it('flags 3 days or fewer as soon', () => {
    expect(PURGE_SOON_DAYS).toBe(3);
  });
});

describe('tidyLabel', () => {
  it('drops a doubled or trailing sentence stop left by an empty part', () => {
    expect(tidyLabel('Dune, Frank. ')).toBe('Dune, Frank');
    expect(tidyLabel('Dune, Frank. . Unverified')).toBe('Dune, Frank. Unverified');
    expect(tidyLabel('Dune、Frank。。未確認')).toBe('Dune、Frank。未確認');
    expect(tidyLabel('Dune, Frank. 2 days. Unverified')).toBe('Dune, Frank. 2 days. Unverified');
  });
});
