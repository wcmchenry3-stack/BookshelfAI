// Initialize i18n with English resources before every test.
// en translations are bundled as static imports and available synchronously;
// components calling useTranslation() will get the correct English strings.
import './src/i18n/i18n';

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

// react-native-webview has no native module under Jest (#740: the hidden
// Turnstile widget). This renders a plain View carrying the WebView's props
// (find it by testID "turnstile-webview") and records ref calls on
// `__mock.injectJavaScript` / `__mock.reload` and each mount (a new native
// instance, e.g. after a `key` change) on `__mock.mount`. Tests drive the
// page by calling the rendered element's `onMessage` prop.
jest.mock('react-native-webview', () => {
  const React = jest.requireActual('react');
  const { View } = jest.requireActual('react-native');
  const injectJavaScript = jest.fn();
  const reload = jest.fn();
  const mount = jest.fn();
  const WebView = React.forwardRef(function MockWebView(
    props: Record<string, unknown>,
    ref: React.Ref<unknown>
  ) {
    // Counted on the first render of each instance (not in an effect, so it
    // does not depend on when passive effects flush under fake timers).
    const mounted = React.useRef(false);
    if (!mounted.current) {
      mounted.current = true;
      mount();
    }
    React.useImperativeHandle(ref, () => ({
      injectJavaScript: (js: string) => injectJavaScript(js),
      reload: () => reload(),
    }));
    return React.createElement(View, props);
  });
  return {
    __esModule: true,
    WebView,
    default: WebView,
    __mock: { injectJavaScript, reload, mount },
  };
});

// expo-font (SDK 58) resolves each font through expo-asset's asset registry, which
// has no entries under Jest, so @expo/vector-icons' componentDidMount font load
// rejected with `Module "N" is missing from the asset registry`. No real fonts load
// in tests: report every family as loaded and make loadAsync a no-op.
jest.mock('expo-font', () => ({
  ...jest.requireActual('expo-font'),
  loadAsync: jest.fn().mockResolvedValue(undefined),
  isLoaded: jest.fn().mockReturnValue(true),
}));

// expo-router cannot load under Jest. Screens get a controllable stand-in with
// useRouter, useLocalSearchParams and useFocusEffect (see __tests__/helpers/routerMock.ts).
// A test file that mocks 'expo-router' itself replaces this one.
jest.mock('expo-router', () => require('./__tests__/helpers/routerMock').routerModule());

// expo-image-manipulator (#668) has no native module under Jest: a stand-in that
// records its calls (see __tests__/helpers/imageManipulatorMock.ts).
jest.mock('expo-image-manipulator', () =>
  require('./__tests__/helpers/imageManipulatorMock').imageManipulatorModule()
);

// AI consent (#1023): consent already given unless a test says otherwise, so
// the scan tests written before the consent gate still upload. Consent tests
// change `aiConsentMock` or `jest.unmock` this and render the real provider
// (see __tests__/helpers/aiConsentMock.ts).
jest.mock('./hooks/useAiConsent', () =>
  require('./__tests__/helpers/aiConsentMock').aiConsentModule()
);

// gesture-handler 3.3 under Jest (#935). With the Reanimated resolver (see
// jest.config.js) Worklets loads its web build, whose `getUIRuntimeHolder`
// throws; gesture-handler's native `installUIRuntimeBindings` calls it in a
// setImmediate when gesture-handler loads, failing the suite after its tests
// ran. Its jestSetup does not cover this module, so use gesture-handler's own
// web variant (a no-op): there is no UI runtime under Jest.
jest.mock('react-native-gesture-handler/src/handlers/gestures/installUIRuntimeBindings', () =>
  jest.requireActual(
    'react-native-gesture-handler/src/handlers/gestures/installUIRuntimeBindings.web'
  )
);
jest.mock(
  'react-native-gesture-handler/lib/module/handlers/gestures/installUIRuntimeBindings',
  () =>
    jest.requireActual(
      'react-native-gesture-handler/lib/module/handlers/gestures/installUIRuntimeBindings.web'
    )
);

// Screen tests render one screen, without the app root's `GestureRoot`
// (`app/_layout.tsx`), so gesture-handler's dev check ("GestureDetector must be
// used as a descendant of GestureHandlerRootView") would fail every screen with
// a swipeable row (#935). Under Jest the root's context therefore defaults to
// "inside a root", as every screen is in the app. Jest cannot model the case
// the check guards on device (a native `<Modal>`'s separate view hierarchy)
// anyway, because React context flows into Modal content under Jest.
// `GestureRoot.test.tsx` covers the real root.
jest.mock('react-native-gesture-handler/src/GestureHandlerRootViewContext', () => ({
  __esModule: true,
  default: jest.requireActual('react').createContext(true),
}));
jest.mock('react-native-gesture-handler/lib/module/GestureHandlerRootViewContext', () => ({
  __esModule: true,
  default: jest.requireActual('react').createContext(true),
}));
