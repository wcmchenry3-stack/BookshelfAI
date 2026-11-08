/**
 * Shared `jest.mock` factories and fixtures for the library screen tests (#816).
 *
 * Like `scanJobMocks.ts`, the factories are `require`d from inside the
 * `jest.mock` call in the test file (babel-jest hoists `jest.mock`):
 *
 *   jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());
 */

/** `useTheme()` returning the real light theme (the library rows read many colour roles). */
export const themeModule = () => ({
  useTheme: () => ({ theme: require('../../theme/light').lightTheme }),
});

/** A `LoadingSpinner` stand-in that renders its message under testID `loading-spinner`. */
export const loadingSpinnerModule = () => ({
  LoadingSpinner: ({ message }: { message?: string }) => {
    const { Text } = require('react-native');
    return <Text testID="loading-spinner">{message ?? 'Loading'}</Text>;
  },
});

/** `expo-router` with `useFocusEffect` (run once on mount), `useRouter` and `useLocalSearchParams`. */
export const focusEffectRouterModule = () => require('./routerMock').routerModule();

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
