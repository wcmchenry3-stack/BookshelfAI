import React, { createContext, useCallback, useMemo, useState } from 'react';

interface BannerAction {
  label: string;
  onPress: () => void;
}

export interface BannerConfig {
  message: string;
  type: 'success' | 'error' | 'info';
  actions?: BannerAction[];
  duration?: number;
  /** Lines of text to show; default 2. 0 shows the whole message. */
  numberOfLines?: number;
}

export interface BannerContextValue {
  banner: BannerConfig | null;
  showBanner: (config: BannerConfig) => void;
  hideBanner: () => void;
}

export const BannerContext = createContext<BannerContextValue>({
  banner: null,
  showBanner: () => {},
  hideBanner: () => {},
});

export function BannerProvider({ children }: { children: React.ReactNode }) {
  const [banner, setBanner] = useState<BannerConfig | null>(null);

  const showBanner = useCallback((config: BannerConfig) => {
    setBanner(config);
  }, []);

  const hideBanner = useCallback(() => {
    setBanner(null);
  }, []);

  const value = useMemo(
    () => ({ banner, showBanner, hideBanner }),
    [banner, showBanner, hideBanner]
  );

  return <BannerContext.Provider value={value}>{children}</BannerContext.Provider>;
}
