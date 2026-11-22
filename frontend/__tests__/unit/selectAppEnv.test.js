/* eslint-env jest, node */
const { spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const SCRIPT = path.join(__dirname, '../../ios/ci_scripts/select_app_env.sh');
const DEV_API_URL = 'https://bookshelfapi-dev.buffingchi.com';

let dir;
const localEnv = () => path.join(dir, '.env.production.local');

function run(env) {
  return spawnSync('bash', [SCRIPT, dir], {
    env: { PATH: process.env.PATH, ...env },
    encoding: 'utf8',
  });
}

beforeEach(() => {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'select-app-env-'));
});

afterEach(() => {
  fs.rmSync(dir, { recursive: true, force: true });
});

describe('select_app_env.sh (#973)', () => {
  it('points a dev branch build at the dev API', () => {
    const result = run({ CI_BRANCH: 'dev' });
    expect(result.status).toBe(0);
    const contents = fs.readFileSync(localEnv(), 'utf8');
    expect(contents).toContain(`EXPO_PUBLIC_API_URL=${DEV_API_URL}\n`);
    expect(contents).toContain('EXPO_PUBLIC_ENVIRONMENT=development\n');
    expect(result.stdout).toContain('non-release build');
  });

  it('points a feature-branch PR into dev at the dev API', () => {
    const result = run({ CI_BRANCH: 'bug/1-x', CI_PULL_REQUEST_TARGET_BRANCH: 'dev' });
    expect(result.status).toBe(0);
    expect(fs.existsSync(localEnv())).toBe(true);
  });

  it.each([
    ['a main branch build', { CI_BRANCH: 'main' }],
    ['a PR into main', { CI_BRANCH: 'dev', CI_PULL_REQUEST_TARGET_BRANCH: 'main' }],
    ['a tag build', { CI_TAG: 'v1.2.0' }],
  ])('keeps .env.production for %s', (_label, env) => {
    const result = run(env);
    expect(result.status).toBe(0);
    expect(fs.existsSync(localEnv())).toBe(false);
    expect(result.stdout).toContain('release build');
  });

  it('removes a stale override before a release build', () => {
    fs.writeFileSync(localEnv(), `EXPO_PUBLIC_API_URL=${DEV_API_URL}\n`);
    expect(run({ CI_BRANCH: 'main' }).status).toBe(0);
    expect(fs.existsSync(localEnv())).toBe(false);
  });

  it('fails when the frontend directory is missing', () => {
    const result = spawnSync('bash', [SCRIPT, path.join(dir, 'nope')], {
      env: { PATH: process.env.PATH, CI_BRANCH: 'dev' },
      encoding: 'utf8',
    });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('frontend directory not found');
  });
});
