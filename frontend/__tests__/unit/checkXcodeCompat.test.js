/* eslint-env jest, node */
const { spawnSync } = require('child_process');
const path = require('path');

const SCRIPT = path.join(__dirname, '../../ios/ci_scripts/check_xcode_compat.sh');
const LOCKFILE = path.join(__dirname, '../../package-lock.json');

function run(env) {
  return spawnSync('bash', [SCRIPT, LOCKFILE], {
    env: { PATH: process.env.PATH, ...env },
    encoding: 'utf8',
  });
}

describe('check_xcode_compat.sh (#708)', () => {
  it('passes on Xcode 26', () => {
    expect(run({ XCODE_VERSION_OVERRIDE: 'Xcode 26.4' }).status).toBe(0);
  });

  it('fails on Xcode 27 with expo-modules-jsi below 58.0.12', () => {
    const result = run({ XCODE_VERSION_OVERRIDE: 'Xcode 27.1', JSI_VERSION_OVERRIDE: '58.0.11' });
    expect(result.status).toBe(1);
    expect(result.stderr).toContain('pin Xcode to the newest 26.x');
  });

  it('passes on Xcode 27 with expo-modules-jsi 58.0.12 or newer', () => {
    for (const version of ['58.0.12', '58.1.0', '59.0.0']) {
      expect(
        run({ XCODE_VERSION_OVERRIDE: 'Xcode 27.1', JSI_VERSION_OVERRIDE: version }).status
      ).toBe(0);
    }
  });

  it('reads the expo-modules-jsi version from package-lock.json', () => {
    const result = run({ XCODE_VERSION_OVERRIDE: 'Xcode 26.4' });
    expect(result.stdout).toMatch(/expo-modules-jsi \d+\.\d+\.\d+/);
  });

  it('can be bypassed with ALLOW_UNSUPPORTED_XCODE=1', () => {
    const result = run({
      XCODE_VERSION_OVERRIDE: 'Xcode 27.1',
      JSI_VERSION_OVERRIDE: '56.0.14',
      ALLOW_UNSUPPORTED_XCODE: '1',
    });
    expect(result.status).toBe(0);
  });

  it('does not block when the Xcode version cannot be read', () => {
    expect(run({ XCODE_VERSION_OVERRIDE: 'something else' }).status).toBe(0);
  });
});
