/**
 * Shared `jest.mock` factories and fixtures for the library screen tests (#816).
 *
 * Like `scanJobMocks.ts`, the factories are `require`d from inside the
 * `jest.mock` call in the test file (babel-jest hoists `jest.mock`):
 *
 *   jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());
 */

/** `useTheme()` with the colour and type tokens the library screens read. */
export const themeModule = () => ({
  useTheme: () => ({
    theme: {
      colors: {
        background: '#fff',
        surface: '#f5f5f5',
        border: '#ccc',
        text: '#000',
        textSecondary: '#888',
        primary: '#0f426f',
        onPrimary: '#ffffff',
        secondary: '#47645d',
        onSurface: '#1b1c1a',
        onSurfaceVariant: '#42474f',
        outline: '#737780',
        secondaryContainer: '#c6e7dd',
        onSecondaryContainer: '#4b6861',
        surfaceContainerLow: '#f5f3ef',
        surfaceContainerHigh: '#eae8e4',
        surfaceContainerHighest: '#e4e2de',
      },
      typography: {
        fontSizeBase: 16,
        fontSizeSM: 14,
        fontSizeLG: 20,
        fontSizeXS: 12,
        fontFamilyHeadline: 'serif',
      },
    },
  }),
});

/** A `LoadingSpinner` stand-in that renders its message under testID `loading-spinner`. */
export const loadingSpinnerModule = () => ({
  LoadingSpinner: ({ message }: { message?: string }) => {
    const { Text } = require('react-native');
    return <Text testID="loading-spinner">{message ?? 'Loading'}</Text>;
  },
});

/** `expo-router` with only `useFocusEffect`, run once on mount. */
export const focusEffectRouterModule = () => ({
  useFocusEffect: (cb: () => void) => {
    require('react').useEffect(cb, []);
  },
});

/** A wishlisted `/user-books` row for Dune; override fields per test. */
export function userBookRow(overrides: Record<string, unknown> = {}) {
  return {
    id: 'ub-1',
    status: 'wishlisted',
    rating: null,
    notes: null,
    wishlisted_at: null,
    purchased_at: null,
    started_at: null,
    finished_at: null,
    book: {
      id: 'b-1',
      title: 'Dune',
      author: 'Frank Herbert',
      cover_url: null,
      description: 'A desert planet epic.',
    },
    edition: { publish_year: 1965, publisher: 'Ace Books', page_count: 604 },
    ...overrides,
  };
}
