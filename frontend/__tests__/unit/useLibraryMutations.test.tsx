import { Alert } from 'react-native';
import { act, renderHook } from '@testing-library/react-native';

import { useLibraryMutations } from '../../hooks/useLibraryMutations';

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

      let first: Promise<void> = Promise.resolve();
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

      let first: Promise<void> = Promise.resolve();
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
      expect(mockShowBanner).not.toHaveBeenCalled();
      expect(onRemoved).not.toHaveBeenCalled();
      expect(result.current.mutatingIds.size).toBe(0);
    });
  });

  it('guards each row independently', async () => {
    let resolvePatch: (v: unknown) => void = () => {};
    mockPatch.mockReturnValueOnce(new Promise((r) => (resolvePatch = r)));
    const { hook } = setup();
    const { result } = await hook;

    let first: Promise<void> = Promise.resolve();
    await act(async () => {
      first = result.current.advanceStatus(ITEM);
      await result.current.advanceStatus({ id: 'ub-2', status: 'wishlisted' });
    });

    expect(mockPatch).toHaveBeenCalledTimes(2);

    await act(async () => {
      resolvePatch({ data: { id: 'ub-1', status: 'purchased' } });
      await first;
    });
  });
});
