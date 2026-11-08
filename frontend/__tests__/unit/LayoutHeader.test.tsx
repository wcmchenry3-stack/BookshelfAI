import fs from 'fs';
import path from 'path';
import RootLayoutDefault from '../../app/_layout';
import React from 'react';
import { render } from '@testing-library/react-native';

jest.mock('../../lib/sentry', () => ({
  Sentry: {
    wrap: jest.fn((component: unknown) => component),
    addBreadcrumb: jest.fn(),
    captureException: jest.fn(),
  },
  initSentry: jest.fn(),
}));

// The mock Stack records the real screenOptions and Stack.Screen options that
// _layout.tsx wires into the navigation header.
const mockStack: {
  screenOptions: Record<string, unknown> | undefined;
  screens: { name: string; options?: Record<string, unknown> }[];
} = { screenOptions: undefined, screens: [] };
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
      isDark: false,
      colors: { background: '#fff', primary: '#0f426f', onPrimary: '#fff' },
      typography: { fontFamilyBody: 'Inter_400Regular' },
    },
    mode: 'light',
    toggleTheme: jest.fn(),
  }),
}));

jest.mock('../../components/ThemeToggleButton', () => {
  const { View } = require('react-native');
  return { ThemeToggleButton: () => <View testID="theme-toggle" /> };
});

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
  NotoSerif_700Bold: 'NotoSerif_700Bold',
  NotoSerif_800ExtraBold: 'NotoSerif_800ExtraBold',
}));
jest.mock('@expo-google-fonts/inter', () => ({
  Inter_400Regular: 'Inter_400Regular',
  Inter_500Medium: 'Inter_500Medium',
  Inter_600SemiBold: 'Inter_600SemiBold',
  Inter_700Bold: 'Inter_700Bold',
}));

describe('RootLayout header wiring (#756, #936)', () => {
  beforeEach(() => {
    mockStack.screens = [];
    mockStack.screenOptions = undefined;
  });

  it('hides the root header on the tab screens', async () => {
    await render(<RootLayoutDefault />);
    const tabs = mockStack.screens.find((x) => x.name === '(tabs)');
    expect(tabs?.options?.headerShown).toBe(false);
  });

  it('no longer puts the logo, theme toggle or feedback "?" in the Stack header', async () => {
    const { queryByTestId } = await render(<RootLayoutDefault />);
    expect(mockStack.screenOptions?.headerLeft).toBeUndefined();
    expect(mockStack.screenOptions?.headerRight).toBeUndefined();
    expect(queryByTestId('theme-toggle')).toBeNull();
    expect(queryByTestId('header-feedback-button')).toBeNull();
  });

  it('no longer ships the floating FeedbackButton FAB module', () => {
    const dir = path.join(__dirname, '../../components/FeedbackWidget');
    const present = fs.existsSync(dir)
      ? fs.readdirSync(dir).filter((f) => f.startsWith('FeedbackButton'))
      : [];
    expect(present).toEqual([]);
  });

  it('removed the header "?" button module (help lives in Settings)', () => {
    const file = path.join(__dirname, '../../components/FeedbackWidget/HeaderFeedbackButton.tsx');
    expect(fs.existsSync(file)).toBe(false);
  });
});
