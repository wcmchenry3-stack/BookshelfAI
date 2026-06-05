// Provide default safe-area insets so useSafeAreaInsets() works without SafeAreaProvider.
jest.mock('react-native-safe-area-context', () => {
  const actual = jest.requireActual('react-native-safe-area-context');
  return {
    ...actual,
    useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
    SafeAreaProvider: ({ children }: { children: React.ReactNode }) => children,
  };
});

// Initialize i18n with English resources before every test.
// en translations are bundled as static imports and available synchronously;
// components calling useTranslation() will get the correct English strings.
import { configure } from '@testing-library/react-native';
import './src/i18n/i18n';

// RNTL v13 switched the default to concurrentRoot:true (React 18 concurrent mode).
// On Linux CI runners this causes waitFor to race against Jest's 5s timeout.
// Restoring legacy root mode keeps the synchronous flush behavior from v12.
configure({ concurrentRoot: false });
