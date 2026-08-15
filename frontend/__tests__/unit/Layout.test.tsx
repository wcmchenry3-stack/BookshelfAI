import React from 'react';
import { render } from '@testing-library/react-native';

// ─── Mocks ───────────────────────────────────────────────────────────────────

// Mock Sentry before importing layout (it self-initialises on import).
// jest.mock factories are hoisted, so we use a shared object that survives
// the hoist and is accessible both in the factory and in test assertions.
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
  const MockStack = ({ children }: { children?: React.ReactNode }) => (
    <View testID="stack">{children}</View>
  );
  MockStack.Screen = () => null;
  return { Stack: MockStack };
});

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: {
        background: '#fff',
        text: '#000',
        iconActive: '#000',
        iconInactive: '#999',
        surface: '#fff',
      },
      spacing: { md: 16 },
    },
    mode: 'light',
    toggleTheme: jest.fn(),
  }),
}));

jest.mock('../../components/BookCandidatePicker', () => ({
  BookCandidatePicker: () => null,
}));

jest.mock('../../components/ErrorBoundary', () => ({
  ErrorBoundary: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

jest.mock('../../components/InAppBanner', () => ({
  InAppBanner: () => null,
}));

jest.mock('../../components/ThemeToggleButton', () => ({
  ThemeToggleButton: () => null,
}));

jest.mock('../../contexts/AuthContext', () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

jest.mock('../../contexts/BannerContext', () => ({
  BannerProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  BannerContext: require('react').createContext({
    banner: null,
    showBanner: jest.fn(),
    hideBanner: jest.fn(),
  }),
}));

jest.mock('../../contexts/ScanJobContext', () => ({
  ScanJobProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  ScanJobContext: require('react').createContext({
    jobs: [],
    reviewingJob: null,
    startScan: jest.fn(),
    retryScan: jest.fn(),
    queueForLater: jest.fn(),
    reviewJob: jest.fn(),
    dismissReview: jest.fn(),
    dismissJob: jest.fn(),
    handleSelectBook: jest.fn(),
  }),
}));

jest.mock('../../hooks/useScanJobs', () => ({
  useScanJobs: () => ({
    reviewingJob: null,
    dismissReview: jest.fn(),
    handleSelectBook: jest.fn(),
  }),
}));

jest.mock('../../theme', () => ({
  ThemeProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

jest.mock('@react-native-community/netinfo', () => ({
  __esModule: true,
  default: {
    addEventListener: jest.fn().mockReturnValue(jest.fn()),
    fetch: jest.fn().mockResolvedValue({ isConnected: true }),
  },
}));

// useFonts must return [true] so the layout renders children instead of <View />
jest.mock('expo-font', () => ({
  useFonts: jest.fn(() => [true]),
}));

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

// ─── Import after mocks ─────────────────────────────────────────────────────

import RootLayoutDefault from '../../app/_layout';

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('RootLayout (_layout.tsx)', () => {
  // These assertions check module-evaluation side effects — they must run
  // before jest.clearAllMocks() so the call history is still intact.
  describe('module evaluation (once per suite)', () => {
    it('wraps the component with Sentry.wrap', () => {
      const { Sentry } = require('../../lib/sentry');
      expect(Sentry.wrap).toHaveBeenCalledWith(expect.any(Function));
    });

    it('adds lifecycle breadcrumb on module evaluation', () => {
      const { Sentry } = require('../../lib/sentry');
      expect(Sentry.addBreadcrumb).toHaveBeenCalledWith(
        expect.objectContaining({
          category: 'app.lifecycle',
          message: 'Root layout module evaluated',
        })
      );
    });
  });

  describe('rendering', () => {
    it('renders without crashing', async () => {
      const { getByTestId } = await render(<RootLayoutDefault />);
      expect(getByTestId('stack')).toBeTruthy();
    });
  });
});
