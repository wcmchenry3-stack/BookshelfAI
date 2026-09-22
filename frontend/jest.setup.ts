// Provide default safe-area insets so useSafeAreaInsets() works without SafeAreaProvider.
jest.mock('react-native-safe-area-context', () => {
  const actual = jest.requireActual('react-native-safe-area-context');
  return {
    ...actual,
    useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
    SafeAreaProvider: ({ children }: { children: React.ReactNode }) => children,
  };
});

// NetInfo has no native module under Jest. The official mock reports a connected
// device by default; tests needing offline behaviour override it with their own
// jest.mock('@react-native-community/netinfo', ...).
jest.mock('@react-native-community/netinfo', () =>
  jest.requireActual('@react-native-community/netinfo/jest/netinfo-mock.js')
);

// Initialize i18n with English resources before every test.
// en translations are bundled as static imports and available synchronously;
// components calling useTranslation() will get the correct English strings.
import './src/i18n/i18n';
