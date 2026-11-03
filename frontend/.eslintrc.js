module.exports = {
  extends: ['expo'],
  plugins: ['react-native-a11y'],
  rules: {
    'react-native-a11y/has-accessibility-props': 'error',
    'react-native-a11y/has-valid-accessibility-role': 'error',
    // #817: the expo preset leaves these as warnings. Lint is warning-free, so they fail the build.
    'import/first': 'error',
    'react/display-name': 'error',
    'react-hooks/exhaustive-deps': 'error',
    'no-restricted-imports': [
      'error',
      {
        paths: [
          {
            name: 'uuid',
            message:
              "Do not import 'uuid' — it requires crypto.getRandomValues() which is unavailable on Hermes and crashes at runtime. Use Crypto.randomUUID() from 'expo-crypto' instead.",
          },
        ],
      },
    ],
  },
  overrides: [
    {
      // The expo preset applies these two TypeScript rules to .ts/.tsx only (plain .js
      // config and scripts keep their CommonJS require); promote them there.
      files: ['*.ts', '*.tsx'],
      rules: {
        '@typescript-eslint/no-unused-vars': 'error',
        '@typescript-eslint/no-require-imports': 'error',
      },
    },
    {
      // Jest hoists jest.mock() above imports, so mock factories and per-test
      // jest.isolateModules() / jest.spyOn(require(...)) / dynamic locale JSON loads must
      // use a lazy require(); an ES import would be evaluated before the mock exists.
      files: ['__tests__/**', 'jest.setup.ts'],
      rules: { '@typescript-eslint/no-require-imports': 'off' },
    },
  ],
};
