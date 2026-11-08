/**
 * A controllable `expo-router` for screen tests. The default mock in
 * `jest.setup.ts` is this module, so every test gets `useRouter`,
 * `useLocalSearchParams` and `useFocusEffect` without a per-file factory.
 *
 * A test reads the calls from `routerState` and sets the route params for the
 * screen under test (e.g. Scan's `?mode=text`) with `routerState.params`.
 * A file with its own `jest.mock('expo-router', ...)` can spread
 * `routerModule()` into it to keep these hooks.
 */
export const routerState = {
  params: {} as Record<string, string | undefined>,
  push: jest.fn(),
  setParams: jest.fn(),
};

const router = {
  push: (...a: unknown[]) => routerState.push(...a),
  setParams: (...a: unknown[]) => routerState.setParams(...a),
  back: jest.fn(),
  replace: jest.fn(),
  canGoBack: () => true,
};

export const routerModule = () => ({
  router,
  useRouter: () => router,
  useLocalSearchParams: () => routerState.params,
  // Runs once on mount, like a screen that gains focus.
  useFocusEffect: (cb: () => void) => {
    jest.requireActual<typeof import('react')>('react').useEffect(cb, []);
  },
  usePathname: () => '/',
  useSegments: () => [],
});
