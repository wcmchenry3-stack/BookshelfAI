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

// AsyncStorage has no native module under Jest. AuthContext imports it through
// lib/freshInstall, so any screen using useAuth needs it. The official mock is an
// in-memory store; tests that assert on storage calls override it with their own
// jest.mock('@react-native-async-storage/async-storage', ...).
jest.mock('@react-native-async-storage/async-storage', () =>
  jest.requireActual('@react-native-async-storage/async-storage/jest/async-storage-mock')
);

// Initialize i18n with English resources before every test.
// en translations are bundled as static imports and available synchronously;
// components calling useTranslation() will get the correct English strings.
import './src/i18n/i18n';
