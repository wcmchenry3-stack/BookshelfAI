// Builds the static web export the e2e suite runs against (#470): into dist/.
//
// It is a DEVELOPMENT bundle (`--dev`) on purpose: the login screen shows its
// "Test Login" button only when `__DEV__` is true (app/(auth)/login.tsx), and
// that button is how the suite signs in without Google. The EXPO_PUBLIC_*
// values are baked into the bundle at export time:
//
// - API URL and test credentials match e2e/stack/run_backend.py,
// - a dummy Google web client id (expo-auth-session throws without one; no
//   Google request is made),
// - no Sentry DSN, and an environment that is not "production".
import { spawnSync } from 'node:child_process';

const apiPort = process.env.E2E_API_PORT ?? '8001';

const result = spawnSync(
  'npx',
  ['expo', 'export', '--platform', 'web', '--dev', '--output-dir', 'dist'],
  {
    stdio: 'inherit',
    shell: process.platform === 'win32',
    env: {
      ...process.env,
      CI: '1',
      EXPO_NO_TELEMETRY: '1',
      EXPO_PUBLIC_API_URL: `http://127.0.0.1:${apiPort}`,
      EXPO_PUBLIC_ENVIRONMENT: 'e2e',
      EXPO_PUBLIC_SENTRY_DSN: '',
      EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID: 'e2e-dummy.apps.googleusercontent.com',
      EXPO_PUBLIC_TEST_AUTH_SECRET: process.env.E2E_TEST_AUTH_SECRET ?? 'e2e-secret',
      EXPO_PUBLIC_TEST_EMAIL: process.env.E2E_TEST_EMAIL ?? 'e2e@example.test',
    },
  }
);
process.exit(result.status ?? 1);
