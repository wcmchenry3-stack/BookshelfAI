/**
 * Jest config (moved out of package.json when SDK 58 / Jest 30 needed a second project).
 *
 * Two projects share one setup and one coverage budget:
 *  - `native`: the jest-expo preset (Node-based React Native environment). Everything
 *    except the files below.
 *  - `web-env`: tests that carry an `@jest-environment jsdom` docblock. Same preset,
 *    but React Native's global setup is swapped for `jest/rnSetupJsdom.js`, which
 *    skips the `window` redefinition that throws inside jsdom.
 * Coverage collection and thresholds are global (root) options and are unchanged.
 */
const { passWithNoTests: _ignored, ...expoPreset } = require('jest-expo/jest-preset');

const WEB_ENV_TESTS = [
  '<rootDir>/__tests__/unit/turnstileWeb.test.ts',
  '<rootDir>/__tests__/unit/ScanTurnstileWeb.test.tsx',
];

const shared = {
  modulePaths: ['<rootDir>/node_modules/expo/node_modules'],
  setupFilesAfterEnv: ['./jest.setup.ts'],
  transformIgnorePatterns: [
    'node_modules/(?!((jest-)?react-native|@react-native(-community)?)|expo(nent)?|@expo(nent)?/.*|@expo-google-fonts/.*|react-navigation|@react-navigation/.*|@unimodules/.*|unimodules|sentry-expo|@sentry/.*|native-base|react-native-svg|i18next|react-i18next|i18next-resources-to-backend)',
  ],
};

const isReactNativeSetup = (file) => file.includes('@react-native/jest-preset/jest/setup.js');

module.exports = {
  collectCoverageFrom: [
    'app/**/*.{ts,tsx}',
    'components/**/*.{ts,tsx}',
    'contexts/**/*.{ts,tsx}',
    'hooks/**/*.{ts,tsx}',
    'lib/**/*.{ts,tsx}',
    'src/i18n/**/*.{ts,tsx}',
    'theme/**/*.{ts,tsx}',
    '!theme/index.ts',
    '!app/(tabs)/_layout.tsx',
    '!app/_layout.tsx',
    '!src/i18n/i18n.ts',
    '!src/i18n/glossary.ts',
  ],
  coverageThreshold: {
    global: { statements: 95, branches: 90, functions: 92, lines: 97 },
  },
  projects: [
    {
      displayName: 'native',
      preset: 'jest-expo',
      ...shared,
      testPathIgnorePatterns: ['/node_modules/', '<rootDir>/__tests__/helpers/', ...WEB_ENV_TESTS],
    },
    {
      displayName: 'web-env',
      ...expoPreset,
      ...shared,
      testEnvironment: require.resolve('./jest/jsdomEnvironment.js'),
      setupFiles: expoPreset.setupFiles.map((file) =>
        isReactNativeSetup(file) ? require.resolve('./jest/rnSetupJsdom.js') : file
      ),
      testMatch: WEB_ENV_TESTS,
    },
  ],
};
