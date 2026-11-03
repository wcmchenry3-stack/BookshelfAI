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
