import { Alert } from 'react-native';
import { renderHook } from '@testing-library/react-native';

import { useOnlineOnly } from '../../hooks/useOnlineOnly';

let mockConnected = true;
jest.mock('../../hooks/useNetworkStatus', () => ({
  useNetworkStatus: () => ({ isConnected: mockConnected }),
}));

beforeEach(() => {
  jest.restoreAllMocks();
  mockConnected = true;
});

describe('useOnlineOnly', () => {
  it('lets the action proceed when online, without alerting', async () => {
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    const { result } = await renderHook(() => useOnlineOnly());

    expect(result.current.isConnected).toBe(true);
    expect(result.current.requireOnline()).toBe(true);
    expect(alertSpy).not.toHaveBeenCalled();
  });

  it('blocks the action and explains why when offline', async () => {
    mockConnected = false;
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    const { result } = await renderHook(() => useOnlineOnly());

    expect(result.current.isConnected).toBe(false);
    expect(result.current.requireOnline()).toBe(false);
    expect(alertSpy).toHaveBeenCalledWith(
      "You're offline",
      'This action requires an internet connection.'
    );
  });
});
