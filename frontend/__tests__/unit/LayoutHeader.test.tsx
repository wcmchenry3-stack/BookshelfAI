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

// The mock Stack renders the real screenOptions.headerRight so the test
// exercises exactly what _layout.tsx wires into the navigation header.
jest.mock('expo-router', () => {
  const { View } = require('react-native');
  const MockStack = ({
    screenOptions,
    children,
  }: {
    screenOptions?: { headerRight?: () => React.ReactNode };
    children?: React.ReactNode;
  }) => (
    <View testID="stack">
      <View testID="header-right">{screenOptions?.headerRight?.()}</View>
      {children}
    </View>
  );
  MockStack.Screen = () => null;
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

import RootLayoutDefault from '../../app/_layout';

describe('RootLayout header wiring (#756)', () => {
  it('puts both the theme toggle and the feedback "?" button in the Stack headerRight', async () => {
    const { getByTestId } = await render(<RootLayoutDefault />);
    const right = getByTestId('header-right');
    expect(right).toContainElement(getByTestId('theme-toggle'));
    expect(right).toContainElement(getByTestId('header-feedback-button'));
  });

  it('renders the feedback button exactly once (no floating duplicate outside the header)', async () => {
    const { getAllByTestId } = await render(<RootLayoutDefault />);
    expect(getAllByTestId('header-feedback-button')).toHaveLength(1);
  });

  it('no longer ships the floating FeedbackButton FAB module', () => {
    expect(() => require.resolve('../../components/FeedbackWidget/FeedbackButton')).toThrow();
  });
});
