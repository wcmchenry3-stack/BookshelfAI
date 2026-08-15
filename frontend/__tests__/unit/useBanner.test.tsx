import React from 'react';
import { renderHook } from '@testing-library/react-native';

import { useBanner } from '../../hooks/useBanner';

const mockContextValue = {
  banner: null,
  showBanner: jest.fn(),
  hideBanner: jest.fn(),
};

jest.mock('../../contexts/BannerContext', () => ({
  BannerContext: require('react').createContext(null),
}));

describe('useBanner', () => {
  it('returns the BannerContext value when wrapped in provider', async () => {
    const { BannerContext } = require('../../contexts/BannerContext');
    const wrapper = ({ children }: { children: React.ReactNode }) => (
      <BannerContext.Provider value={mockContextValue}>{children}</BannerContext.Provider>
    );
    const { result } = await renderHook(() => useBanner(), { wrapper });
    expect(result.current).toBe(mockContextValue);
  });

  it('returns null when no provider is present', async () => {
    const { result } = await renderHook(() => useBanner());
    expect(result.current).toBeNull();
  });
});
