/* eslint-env jest, node */
const { spawnSync } = require('child_process');
const path = require('path');

const SCRIPT = path.join(__dirname, '../../ios/ci_scripts/check_xcode_compat.sh');
const FIXTURES = path.join(__dirname, '../fixtures/xcode-compat');
const fixture = (name) => path.join(FIXTURES, name);

function run(lockfile, env) {
  return spawnSync('bash', [SCRIPT, lockfile], {
    env: { PATH: process.env.PATH, ...env },
    encoding: 'utf8',
  });
}

describe('check_xcode_compat.sh (#708)', () => {
  it('blocks Xcode 27 with the SDK 56 lockfile', () => {
    const result = run(fixture('sdk56.json'), { XCODE_VERSION_OVERRIDE: 'Xcode 27.0' });
    expect(result.status).toBe(1);
    expect(result.stdout).toContain('expo-modules-jsi 56.0.14');
    expect(result.stderr).toContain('pin Xcode to the newest 26.x');
  });

  it('blocks any later Xcode major too', () => {
    expect(run(fixture('sdk56.json'), { XCODE_VERSION_OVERRIDE: 'Xcode 28' }).status).toBe(1);
  });

  it('allows Xcode 26 with the SDK 56 lockfile', () => {
    expect(run(fixture('sdk56.json'), { XCODE_VERSION_OVERRIDE: 'Xcode 26.6' }).status).toBe(0);
  });

  it('allows Xcode 27 with the SDK 58 lockfile', () => {
    expect(run(fixture('sdk58.json'), { XCODE_VERSION_OVERRIDE: 'Xcode 27.1' }).status).toBe(0);
  });

  it('uses the lowest version when a nested copy is older', () => {
    const result = run(fixture('nested-older.json'), { XCODE_VERSION_OVERRIDE: 'Xcode 27.1' });
    expect(result.status).toBe(1);
    expect(result.stdout).toContain('expo-modules-jsi 56.0.14');
  });

  it('compares the numeric core of a prerelease version', () => {
    const result = run(fixture('prerelease.json'), { XCODE_VERSION_OVERRIDE: 'Xcode 27.1' });
    expect(result.status).toBe(1);
    expect(result.stdout).toContain('expo-modules-jsi 56.0.14');
  });

  it('compares versions numerically around the 58.0.12 floor', () => {
    const cases = { '58.0.11': 1, '57.9.99': 1, '58.0.12': 0, '58.0.100': 0, '59.0.0': 0 };
    for (const [version, status] of Object.entries(cases)) {
      const env = { XCODE_VERSION_OVERRIDE: 'Xcode 27.1', JSI_VERSION_OVERRIDE: version };
      expect(run(fixture('sdk56.json'), env).status).toBe(status);
    }
  });

  it('fails when the lockfile is missing', () => {
    const result = run(fixture('missing.json'), { XCODE_VERSION_OVERRIDE: 'Xcode 26.6' });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('lockfile not found');
  });

  it('does not block when expo-modules-jsi is absent or Xcode is unreadable', () => {
    expect(run(fixture('no-jsi.json'), { XCODE_VERSION_OVERRIDE: 'Xcode 27.1' }).status).toBe(0);
    expect(run(fixture('sdk56.json'), { XCODE_VERSION_OVERRIDE: 'something else' }).status).toBe(0);
  });

  it('can be bypassed with ALLOW_UNSUPPORTED_XCODE=1', () => {
    const env = { XCODE_VERSION_OVERRIDE: 'Xcode 27.1', ALLOW_UNSUPPORTED_XCODE: '1' };
    expect(run(fixture('sdk56.json'), env).status).toBe(0);
  });

  it('reads the real package-lock.json', () => {
    const lockfile = path.join(__dirname, '../../package-lock.json');
    const result = run(lockfile, { XCODE_VERSION_OVERRIDE: 'Xcode 26.6' });
    expect(result.status).toBe(0);
    expect(result.stdout).toMatch(/expo-modules-jsi \d+\.\d+\.\d+/);
  });
});
