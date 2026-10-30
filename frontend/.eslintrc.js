module.exports = {
  extends: ['expo'],
  ignorePatterns: ['vendor/'],
  plugins: ['react-native-a11y'],
  rules: {
    'react-native-a11y/has-accessibility-props': 'error',
    'react-native-a11y/has-valid-accessibility-role': 'error',
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
};
