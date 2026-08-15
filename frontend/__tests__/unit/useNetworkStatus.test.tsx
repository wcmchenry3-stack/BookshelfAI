import { renderHook, act } from '@testing-library/react-native';

let capturedListener: ((state: { isConnected: boolean | null }) => void) | null = null;
const mockUnsubscribe = jest.fn();

jest.mock('@react-native-community/netinfo', () => ({
  __esModule: true,
  default: {
    addEventListener: (cb: (state: { isConnected: boolean | null }) => void) => {
      capturedListener = cb;
      return mockUnsubscribe;
    },
  },
}));

import { useNetworkStatus } from '../../hooks/useNetworkStatus';

beforeEach(() => {
  capturedListener = null;
  mockUnsubscribe.mockClear();
});

describe('useNetworkStatus', () => {
  it('defaults to connected', async () => {
    const { result } = await renderHook(() => useNetworkStatus());
    expect(result.current.isConnected).toBe(true);
  });

  it('updates when connection is lost', async () => {
    const { result } = await renderHook(() => useNetworkStatus());

    await act(() => {
      capturedListener?.({ isConnected: false });
    });

    expect(result.current.isConnected).toBe(false);
  });

  it('updates when connection is restored', async () => {
    const { result } = await renderHook(() => useNetworkStatus());

    await act(() => {
      capturedListener?.({ isConnected: false });
    });
    expect(result.current.isConnected).toBe(false);

    await act(() => {
      capturedListener?.({ isConnected: true });
    });
    expect(result.current.isConnected).toBe(true);
  });

  it('treats null isConnected as true (fallback)', async () => {
    const { result } = await renderHook(() => useNetworkStatus());

    await act(() => {
      capturedListener?.({ isConnected: null });
    });

    expect(result.current.isConnected).toBe(true);
  });

  it('cleans up listener on unmount', async () => {
    const { unmount } = await renderHook(() => useNetworkStatus());
    await unmount();
    expect(mockUnsubscribe).toHaveBeenCalled();
  });
});
