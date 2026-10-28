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
// `__mock.injectJavaScript` / `__mock.reload`. Tests drive the page by
// calling the rendered element's `onMessage` prop.
jest.mock('react-native-webview', () => {
  const React = jest.requireActual('react');
  const { View } = jest.requireActual('react-native');
  const injectJavaScript = jest.fn();
  const reload = jest.fn();
  const WebView = React.forwardRef(function MockWebView(
    props: Record<string, unknown>,
    ref: React.Ref<unknown>
  ) {
    React.useImperativeHandle(ref, () => ({
      injectJavaScript: (js: string) => injectJavaScript(js),
      reload: () => reload(),
    }));
    return React.createElement(View, props);
  });
  return { __esModule: true, WebView, default: WebView, __mock: { injectJavaScript, reload } };
});

// Initialize i18n with English resources before every test.
// en translations are bundled as static imports and available synchronously;
// components calling useTranslation() will get the correct English strings.
import './src/i18n/i18n';
