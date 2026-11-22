import fs from 'fs';
import path from 'path';
import React from 'react';
import { render } from '@testing-library/react-native';
import RootLayoutDefault from '../../app/_layout';

let mockIsDark = true;
const mockStack: {
  screenOptions?: Record<string, unknown>;
  screens: { name: string; options?: Record<string, unknown> }[];
} = { screens: [] };

jest.mock('../../lib/sentry', () => ({
  Sentry: {
    wrap: jest.fn((component: unknown) => component),
    addBreadcrumb: jest.fn(),
    captureException: jest.fn(),
  },
  initSentry: jest.fn(),
}));

jest.mock('expo-router', () => {
  const { View } = require('react-native');
  const MockStack = ({
    screenOptions,
    children,
  }: {
    screenOptions?: Record<string, unknown>;
    children?: React.ReactNode;
  }) => {
    mockStack.screenOptions = screenOptions;
    return <View testID="stack">{children}</View>;
  };
  MockStack.Screen = function MockScreen(props: {
    name: string;
    options?: Record<string, unknown>;
  }) {
    mockStack.screens.push(props);
    return null;
  };
  return { Stack: MockStack };
});

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      isDark: mockIsDark,
      colors: { background: '#abcdef', primary: '#111', headerOverlay: 'rgba(0,0,0,0.5)' },
    },
    mode: mockIsDark ? 'dark' : 'light',
    toggleTheme: jest.fn(),
  }),
}));

jest.mock('../../components/ThemeToggleButton', () => ({ ThemeToggleButton: () => null }));
jest.mock('../../components/BookCandidatePicker', () => ({ BookCandidatePicker: () => null }));
jest.mock('../../components/AddUnverifiedBookSheet', () => ({
  AddUnverifiedBookSheet: () => null,
}));
jest.mock('../../components/ErrorBoundary', () => ({
  ErrorBoundary: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
jest.mock('../../components/InAppBanner', () => ({ InAppBanner: () => null }));
jest.mock('../../components/FeedbackWidget/FeedbackContext', () => ({
  FeedbackProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useFeedback: () => ({ openFeedback: jest.fn() }),
}));
jest.mock('../../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
jest.mock('../../contexts/BannerContext', () => ({
  BannerProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
jest.mock('../../contexts/ScanJobContext', () => ({
  ScanJobProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
jest.mock('../../contexts/TurnstileContext', () => ({
  TurnstileProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
jest.mock('../../hooks/useScanJobs', () => ({
  useScanJobs: () => ({ reviewingJob: null, dismissReview: jest.fn(), unverifiedDrafts: [] }),
}));
jest.mock('../../theme', () => ({
  ThemeProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
jest.mock('expo-font', () => ({ useFonts: jest.fn(() => [true]) }));
jest.mock('@expo-google-fonts/noto-serif', () => ({
  NotoSerif_700Bold: 'a',
  NotoSerif_800ExtraBold: 'b',
}));
jest.mock('@expo-google-fonts/inter', () => ({
  Inter_400Regular: 'a',
  Inter_500Medium: 'b',
  Inter_600SemiBold: 'c',
  Inter_700Bold: 'd',
}));

describe('root Stack chrome (#906, #908)', () => {
  beforeEach(() => {
    mockStack.screens = [];
  });

  it.each([true, false])('header is opaque, not transparent (isDark=%s)', async (dark) => {
    mockIsDark = dark;
    await render(<RootLayoutDefault />);
    const opts = mockStack.screenOptions as {
      headerTransparent: boolean;
      headerShadowVisible: boolean;
      headerStyle: { backgroundColor: string };
    };
    expect(opts.headerTransparent).toBe(false);
    expect(opts.headerShadowVisible).toBe(false);
    expect(opts.headerStyle.backgroundColor).toBe('#abcdef');
    expect(JSON.stringify(opts.headerStyle)).not.toContain('backdropFilter');
  });

  it('hides the header on the real login route so no route name is shown', async () => {
    mockIsDark = false;
    await render(<RootLayoutDefault />);
    // Every registered screen name must be a route file (or a group dir with its
    // own _layout), else expo-router silently ignores the options.
    const appDir = path.join(__dirname, '../../app');
    const routeExists = (name: string) =>
      fs.existsSync(path.join(appDir, `${name}.tsx`)) ||
      fs.existsSync(path.join(appDir, name, '_layout.tsx'));
    for (const s of mockStack.screens) expect(routeExists(s.name)).toBe(true);

    const login = mockStack.screens.find((s) => s.name === '(auth)/login');
    expect(login?.options?.headerShown).toBe(false);
    for (const s of mockStack.screens) {
      const title = s.options?.title;
      if (typeof title === 'string') expect(title).not.toMatch(/[()/]/);
    }
  });
});
