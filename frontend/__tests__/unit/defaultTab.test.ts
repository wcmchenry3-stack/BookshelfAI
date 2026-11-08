import {
  DEFAULT_TABS,
  DEFAULT_TAB_FETCH_TIMEOUT_MS,
  FALLBACK_DEFAULT_TAB,
  defaultTabFromMe,
  defaultTabRoute,
  fetchDefaultTab,
  isDefaultTab,
  loadDefaultTab,
  parseDefaultTab,
  saveDefaultTab,
} from '../../lib/defaultTab';

const mockGet = jest.fn();
const mockPatch = jest.fn();
jest.mock('../../lib/api', () => ({
  api: {
    get: (...a: unknown[]) => mockGet(...a),
    patch: (...a: unknown[]) => mockPatch(...a),
  },
}));

beforeEach(() => {
  jest.clearAllMocks();
});

describe('default tab values (#913)', () => {
  it('matches the backend DEFAULT_TABS and falls back to Scan', () => {
    expect(DEFAULT_TABS).toEqual(['scan', 'wishlist', 'my-books']);
    expect(FALLBACK_DEFAULT_TAB).toBe('scan');
  });

  it.each([
    ['scan', '/(tabs)/scan'],
    ['wishlist', '/(tabs)/wishlist'],
    ['my-books', '/(tabs)/my-books'],
  ])('routes %s to %s', (tab, route) => {
    expect(isDefaultTab(tab)).toBe(true);
    expect(parseDefaultTab(tab)).toBe(tab);
    expect(defaultTabRoute(tab)).toBe(route);
  });

  it.each([null, undefined, '', 'settings', 'Scan', 'my_books', 3, {}, ['scan']])(
    'routes %p to Scan',
    (value) => {
      expect(isDefaultTab(value)).toBe(false);
      expect(parseDefaultTab(value)).toBe('scan');
      expect(defaultTabRoute(value)).toBe('/(tabs)/scan');
    }
  );

  it.each([
    [{ default_tab: 'wishlist' }, 'wishlist'],
    [{ default_tab: null }, 'scan'],
    [{}, 'scan'],
    [null, 'scan'],
    ['wishlist', 'scan'],
  ])('reads %p from a /auth/me body as %s', (body, tab) => {
    expect(defaultTabFromMe(body)).toBe(tab);
  });
});

describe('loadDefaultTab / fetchDefaultTab', () => {
  it('returns the stored tab from GET /auth/me', async () => {
    mockGet.mockResolvedValue({ data: { default_tab: 'my-books' } });
    await expect(loadDefaultTab()).resolves.toBe('my-books');
    expect(mockGet).toHaveBeenCalledWith('/auth/me', { timeout: DEFAULT_TAB_FETCH_TIMEOUT_MS });
    await expect(fetchDefaultTab()).resolves.toBe('my-books');
  });

  it('is null (load) / Scan (fetch) when the call fails', async () => {
    mockGet.mockRejectedValue({ message: 'Network Error' });
    await expect(loadDefaultTab()).resolves.toBeNull();
    await expect(fetchDefaultTab()).resolves.toBe('scan');
  });

  it('never rejects when the client throws synchronously or returns nothing', async () => {
    mockGet.mockImplementation(() => {
      throw new Error('boom');
    });
    await expect(loadDefaultTab()).resolves.toBeNull();
    mockGet.mockReturnValue(undefined);
    await expect(loadDefaultTab()).resolves.toBeNull();
  });

  it('gives up after the timeout', async () => {
    jest.useFakeTimers();
    try {
      mockGet.mockReturnValue(new Promise(() => {}));
      const loaded = loadDefaultTab(1000);
      const fetched = fetchDefaultTab(1000);
      jest.advanceTimersByTime(1000);
      await expect(loaded).resolves.toBeNull();
      await expect(fetched).resolves.toBe('scan');
    } finally {
      jest.useRealTimers();
    }
  });
});

describe('saveDefaultTab', () => {
  it('PATCHes /users/me/preferences and returns what the server stored', async () => {
    mockPatch.mockResolvedValue({ data: { default_tab: 'wishlist' } });
    await expect(saveDefaultTab('wishlist')).resolves.toBe('wishlist');
    expect(mockPatch).toHaveBeenCalledWith('/users/me/preferences', { default_tab: 'wishlist' });
  });

  it('rejects when the save fails', async () => {
    mockPatch.mockRejectedValue({ response: { status: 500 } });
    await expect(saveDefaultTab('scan')).rejects.toEqual({ response: { status: 500 } });
  });
});
