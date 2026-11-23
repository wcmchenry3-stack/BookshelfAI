import { Alert } from 'react-native';
import { act, renderHook } from '@testing-library/react-native';

import { useLibraryMutations } from '../../hooks/useLibraryMutations';
import { onLibraryChanged } from '../../lib/libraryChanged';

const mockPatch = jest.fn();
const mockDelete = jest.fn();
const mockShowBanner = jest.fn();
const mockOpenTrash = jest.fn();
let mockOnline = true;

jest.mock('../../lib/api', () => ({
  api: {
    patch: (...args: unknown[]) => mockPatch(...args),
    delete: (...args: unknown[]) => mockDelete(...args),
  },
}));

// The en copy for my-books and wishlist is identical, so echo "<ns>:<key>" to
// prove which namespace the hook reads.
jest.mock('react-i18next', () => ({
  useTranslation: (ns: string) => ({
    t: (key: string, opts?: { ns?: string }) => `${opts?.ns ?? ns}:${key}`,
  }),
}));

jest.mock('../../lib/trash', () => ({
  openTrash: (...args: unknown[]) => mockOpenTrash(...args),
}));

jest.mock('../../hooks/useBanner', () => ({
  useBanner: () => ({ showBanner: (...args: unknown[]) => mockShowBanner(...args) }),
}));

jest.mock('../../hooks/useOnlineOnly', () => ({
  useOnlineOnly: () => ({ isConnected: mockOnline, requireOnline: () => mockOnline }),
}));

interface Item {
  id: string;
  status: string;
}

const ITEM: Item = { id: 'ub-1', status: 'wishlisted' };

function setup(overrides: Partial<Parameters<typeof useLibraryMutations<Item>>[0]> = {}) {
  const onAdvanced = jest.fn();
  const onRemoved = jest.fn();
  const hook = renderHook(() =>
    useLibraryMutations<Item>({
      namespace: 'my-books',
      nextStatus: (item) => (item.status === 'wishlisted' ? 'purchased' : undefined),
      onAdvanced,
      onRemoved,
      ...overrides,
    })
  );
  return { hook, onAdvanced, onRemoved };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockOnline = true;
  mockPatch.mockResolvedValue({ data: { id: 'ub-1', status: 'purchased' } });
  mockDelete.mockResolvedValue({});
  jest.spyOn(Alert, 'alert').mockImplementation(() => {});
});

describe('useLibraryMutations', () => {
  describe('advanceStatus', () => {
    it('patches the next status and reports the server response', async () => {
      const { hook, onAdvanced } = setup();
      const { result } = await hook;

      await act(async () => {
        await result.current.advanceStatus(ITEM);
      });

      expect(mockPatch).toHaveBeenCalledWith('/user-books/ub-1', { status: 'purchased' });
      expect(onAdvanced).toHaveBeenCalledWith(ITEM, { id: 'ub-1', status: 'purchased' });
      expect(result.current.mutatingIds.size).toBe(0);
    });

    it('does nothing when the item has no next status', async () => {
      const { hook, onAdvanced } = setup();
      const { result } = await hook;

      await act(async () => {
        await result.current.advanceStatus({ id: 'ub-9', status: 'read' });
      });

      expect(mockPatch).not.toHaveBeenCalled();
      expect(onAdvanced).not.toHaveBeenCalled();
    });

    it('does nothing when offline', async () => {
      mockOnline = false;
      const { hook, onAdvanced } = setup();
      const { result } = await hook;

      await act(async () => {
        await result.current.advanceStatus(ITEM);
      });

      expect(mockPatch).not.toHaveBeenCalled();
      expect(onAdvanced).not.toHaveBeenCalled();
    });

    it('blocks a second press while the first is in flight', async () => {
      let resolvePatch: (v: unknown) => void = () => {};
      mockPatch.mockReturnValue(new Promise((r) => (resolvePatch = r)));
      const { hook, onAdvanced } = setup();
      const { result } = await hook;

      let first: Promise<unknown> = Promise.resolve();
      await act(async () => {
        // Both presses are handled before a re-render, as with a fast double tap.
        first = result.current.advanceStatus(ITEM);
        await result.current.advanceStatus(ITEM);
      });

      expect(mockPatch).toHaveBeenCalledTimes(1);
      expect(result.current.mutatingIds.has('ub-1')).toBe(true);

      await act(async () => {
        resolvePatch({ data: { id: 'ub-1', status: 'purchased' } });
        await first;
      });

      expect(onAdvanced).toHaveBeenCalledTimes(1);
      expect(result.current.mutatingIds.has('ub-1')).toBe(false);
    });

    it('alerts, leaves the list alone and releases the guard on failure', async () => {
      mockPatch.mockRejectedValue(new Error('boom'));
      const { hook, onAdvanced } = setup();
      const { result } = await hook;

      await act(async () => {
        await result.current.advanceStatus(ITEM);
      });

      expect(Alert.alert).toHaveBeenCalledTimes(1);
      expect(Alert.alert).toHaveBeenCalledWith('common:errorTitle', 'my-books:errorUpdateStatus');
      expect(onAdvanced).not.toHaveBeenCalled();
      expect(result.current.mutatingIds.size).toBe(0);

      // The guard was released, so a retry goes through.
      mockPatch.mockResolvedValue({ data: { id: 'ub-1', status: 'purchased' } });
      await act(async () => {
        await result.current.advanceStatus(ITEM);
      });
      expect(onAdvanced).toHaveBeenCalledTimes(1);
    });
  });

  describe('removeBook', () => {
    it('deletes, shows the moved-to-trash banner with a trash action, then reports', async () => {
      const { hook, onRemoved } = setup();
      const { result } = await hook;

      await act(async () => {
        await result.current.removeBook(ITEM);
      });

      expect(mockDelete).toHaveBeenCalledWith('/user-books/ub-1');
      expect(mockShowBanner).toHaveBeenCalledTimes(1);
      const banner = mockShowBanner.mock.calls[0][0];
      expect(banner.type).toBe('success');
      expect(banner.actions).toHaveLength(1);
      banner.actions[0].onPress();
      expect(mockOpenTrash).toHaveBeenCalledTimes(1);
      expect(onRemoved).toHaveBeenCalledWith(ITEM);
      expect(result.current.mutatingIds.size).toBe(0);
    });

    it('does nothing when offline', async () => {
      mockOnline = false;
      const { hook, onRemoved } = setup();
      const { result } = await hook;

      await act(async () => {
        await result.current.removeBook(ITEM);
      });

      expect(mockDelete).not.toHaveBeenCalled();
      expect(onRemoved).not.toHaveBeenCalled();
    });

    it('blocks a second press while the first is in flight', async () => {
      let resolveDelete: (v: unknown) => void = () => {};
      mockDelete.mockReturnValue(new Promise((r) => (resolveDelete = r)));
      const { hook, onRemoved } = setup();
      const { result } = await hook;

      let first: Promise<unknown> = Promise.resolve();
      await act(async () => {
        first = result.current.removeBook(ITEM);
        await result.current.removeBook(ITEM);
      });

      expect(mockDelete).toHaveBeenCalledTimes(1);

      await act(async () => {
        resolveDelete({});
        await first;
      });

      expect(onRemoved).toHaveBeenCalledTimes(1);
    });

    it('alerts, shows no banner and releases the guard on failure', async () => {
      mockDelete.mockRejectedValue(new Error('boom'));
      const { hook, onRemoved } = setup();
      const { result } = await hook;

      await act(async () => {
        await result.current.removeBook(ITEM);
      });

      expect(Alert.alert).toHaveBeenCalledTimes(1);
      expect(Alert.alert).toHaveBeenCalledWith('common:errorTitle', 'my-books:errorRemoveBook');
      expect(mockShowBanner).not.toHaveBeenCalled();
      expect(onRemoved).not.toHaveBeenCalled();
      expect(result.current.mutatingIds.size).toBe(0);
    });

    it('resolves true only when the server removed the book (#935)', async () => {
      const { hook } = setup();
      const { result } = await hook;
      const outcomes: boolean[] = [];

      await act(async () => {
        outcomes.push(await result.current.removeBook(ITEM));
      });
      mockDelete.mockRejectedValueOnce(new Error('boom'));
      await act(async () => {
        outcomes.push(await result.current.removeBook(ITEM));
      });

      let resolveDelete: (v: unknown) => void = () => {};
      mockDelete.mockReturnValueOnce(new Promise((r) => (resolveDelete = r)));
      let first: Promise<boolean> = Promise.resolve(false);
      await act(async () => {
        first = result.current.removeBook(ITEM);
        // Already in flight: refused at once.
        outcomes.push(await result.current.removeBook(ITEM));
      });
      await act(async () => {
        resolveDelete({});
        outcomes.push(await first);
      });

      expect(outcomes).toEqual([true, false, false, true]);
    });

    it('resolves false offline', async () => {
      mockOnline = false;
      const { hook } = setup();
      const { result } = await hook;
      let outcome: boolean | undefined;
      await act(async () => {
        outcome = await result.current.removeBook(ITEM);
      });
      expect(outcome).toBe(false);
    });
  });

  it('uses the wishlist copy for failures when namespace is wishlist', async () => {
    mockPatch.mockRejectedValue(new Error('boom'));
    mockDelete.mockRejectedValue(new Error('boom'));
    const { hook } = setup({ namespace: 'wishlist' });
    const { result } = await hook;

    await act(async () => {
      await result.current.advanceStatus(ITEM);
    });
    expect(Alert.alert).toHaveBeenLastCalledWith('common:errorTitle', 'wishlist:errorUpdateStatus');

    await act(async () => {
      await result.current.removeBook(ITEM);
    });
    expect(Alert.alert).toHaveBeenLastCalledWith('common:errorTitle', 'wishlist:errorRemoveBook');
  });

  it('guards each row independently', async () => {
    const resolvers: ((v: unknown) => void)[] = [];
    mockPatch.mockImplementation(() => new Promise((r) => resolvers.push(r)));
    const { hook } = setup();
    const { result } = await hook;

    let both: Promise<unknown> = Promise.resolve();
    await act(async () => {
      both = Promise.all([
        result.current.advanceStatus(ITEM),
        result.current.advanceStatus({ id: 'ub-2', status: 'wishlisted' }),
      ]);
    });

    expect(mockPatch).toHaveBeenCalledTimes(2);
    expect(result.current.mutatingIds.has('ub-1')).toBe(true);
    expect(result.current.mutatingIds.has('ub-2')).toBe(true);

    await act(async () => {
      resolvers.forEach((r) => r({ data: { id: 'x', status: 'purchased' } }));
      await both;
    });
    expect(result.current.mutatingIds.size).toBe(0);
  });
});

describe('entry versions (#685)', () => {
  const VERSIONED = { id: 'ub-1', status: 'wishlisted', version: 3 };
  const IF_MATCH = { headers: { 'If-Match': '"3"' } };

  function conflict(code = 'version_mismatch') {
    return Object.assign(new Error('Request failed with status code 412'), {
      response: { status: 412, data: { detail: { code, current_version: 4 } }, headers: {} },
    });
  }

  let unsubscribe: () => void = () => {};
  const libraryChanged = jest.fn();
  beforeEach(() => {
    unsubscribe = onLibraryChanged(libraryChanged);
  });
  afterEach(() => unsubscribe());

  it('sends the cached version as If-Match on PATCH and DELETE', async () => {
    const { hook } = setup();
    const { result } = await hook;

    await act(async () => {
      await result.current.advanceStatus(VERSIONED);
      await result.current.removeBook(VERSIONED);
    });

    expect(mockPatch).toHaveBeenCalledWith('/user-books/ub-1', { status: 'purchased' }, IF_MATCH);
    expect(mockDelete).toHaveBeenCalledWith('/user-books/ub-1', IF_MATCH);
  });

  it('on a 412 PATCH: shows the message, reloads, rolls nothing forward, no retry', async () => {
    mockPatch.mockRejectedValue(conflict());
    const onConflict = jest.fn();
    const { hook, onAdvanced } = setup({ onConflict });
    const { result } = await hook;

    await act(async () => {
      await result.current.advanceStatus(VERSIONED);
    });

    expect(mockShowBanner).toHaveBeenCalledWith({
      message: 'my-books:changedOnAnotherDevice',
      type: 'info',
    });
    expect(libraryChanged).toHaveBeenCalledTimes(1);
    expect(onConflict).toHaveBeenCalledWith(VERSIONED);
    expect(onAdvanced).not.toHaveBeenCalled();
    expect(Alert.alert).not.toHaveBeenCalled();
    expect(mockPatch).toHaveBeenCalledTimes(1);
    expect(result.current.mutatingIds.size).toBe(0);
  });

  it('on a 412 DELETE: shows the message and reloads instead of the trash banner', async () => {
    mockDelete.mockRejectedValue(conflict());
    const { hook, onRemoved } = setup({ namespace: 'wishlist' });
    const { result } = await hook;

    await act(async () => {
      await result.current.removeBook(VERSIONED);
    });

    expect(mockShowBanner).toHaveBeenCalledTimes(1);
    expect(mockShowBanner.mock.calls[0][0].message).toBe('my-books:changedOnAnotherDevice');
    expect(libraryChanged).toHaveBeenCalledTimes(1);
    expect(onRemoved).not.toHaveBeenCalled();
    expect(Alert.alert).not.toHaveBeenCalled();
    expect(mockDelete).toHaveBeenCalledTimes(1);
  });

  it('a 412 DELETE resolves false: the book was not removed (#935)', async () => {
    mockDelete.mockRejectedValue(conflict());
    const { hook } = setup({ namespace: 'wishlist' });
    const { result } = await hook;
    let outcome: boolean | undefined;
    await act(async () => {
      outcome = await result.current.removeBook(VERSIONED);
    });
    expect(outcome).toBe(false);
  });

  it('treats any other 412 as an ordinary failure', async () => {
    mockPatch.mockRejectedValue(conflict('something_else'));
    const { hook } = setup();
    const { result } = await hook;

    await act(async () => {
      await result.current.advanceStatus(VERSIONED);
    });

    expect(Alert.alert).toHaveBeenCalledWith('common:errorTitle', 'my-books:errorUpdateStatus');
    expect(mockShowBanner).not.toHaveBeenCalled();
    expect(libraryChanged).not.toHaveBeenCalled();
  });
});

describe('a write applied without its response (#685)', () => {
  const outcomeUnknown = () => ({
    response: { status: 409, data: { detail: 'idempotency_outcome_unknown' } },
  });
  let reloads: jest.Mock;
  let unsubscribe: () => void;

  beforeEach(() => {
    reloads = jest.fn();
    unsubscribe = onLibraryChanged(reloads);
  });
  afterEach(() => unsubscribe());

  it.each(['advanceStatus', 'removeBook'] as const)(
    '%s: reloads the lists, shows no error and does not retry',
    async (action) => {
      mockPatch.mockRejectedValue(outcomeUnknown());
      mockDelete.mockRejectedValue(outcomeUnknown());
      const { hook, onAdvanced, onRemoved } = setup();
      const { result } = await hook;

      await act(async () => {
        await result.current[action](ITEM);
      });

      expect(reloads).toHaveBeenCalledTimes(1);
      expect(Alert.alert).not.toHaveBeenCalled();
      expect(mockShowBanner).not.toHaveBeenCalled();
      expect(onAdvanced).not.toHaveBeenCalled();
      expect(onRemoved).not.toHaveBeenCalled();
      expect(mockPatch.mock.calls.length + mockDelete.mock.calls.length).toBe(1);
    }
  );
});
