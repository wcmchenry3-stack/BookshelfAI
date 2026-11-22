/* eslint-env jest, node */
const { spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const {
  applySentryNodeEnv,
  applyToXcodeProject,
  BEGIN,
  END,
  PHASE_NAME,
} = require('../../plugins/withSentryDebugFilesNodeEnv');

// Exactly what @sentry/react-native 8.28's getDebugFilesUploadScript() generates.
const SENTRY_SCRIPT =
  "/bin/sh \"`${NODE_BINARY:-node} --print \"require('path').dirname(require.resolve('@sentry/react-native/package.json')) + '/scripts/sentry-xcode-debug-files.sh'\"`\"";
const SENTRY_SCRIPT_NO_UPLOAD = `export SENTRY_DISABLE_AUTO_UPLOAD=true\n${SENTRY_SCRIPT}`;

const PBXPROJ = path.join(__dirname, '../../ios/BookShelfAI.xcodeproj/project.pbxproj');

function fakeProject(shellScript) {
  const phase = { isa: 'PBXShellScriptBuildPhase', shellScript };
  return {
    phase,
    pbxItemByComment: (comment, type) =>
      comment === PHASE_NAME && type === 'PBXShellScriptBuildPhase' ? phase : undefined,
  };
}

// Decoded shellScript of the "Upload Debug Symbols to Sentry" phase in the
// committed project. The `xcode` library writes strings JSON-encoded.
function committedPhaseScript() {
  const pbx = fs.readFileSync(PBXPROJ, 'utf8');
  const m = pbx.match(
    /name = "Upload Debug Symbols to Sentry";[\s\S]*?\n\t+shellScript = ("(?:[^"\\]|\\.)*");/
  );
  if (!m) throw new Error('Upload Debug Symbols to Sentry phase not found in project.pbxproj');
  return JSON.parse(m[1]);
}

describe('withSentryDebugFilesNodeEnv', () => {
  it('prepends the env prelude and keeps the Sentry command unchanged', () => {
    const out = applySentryNodeEnv(SENTRY_SCRIPT);

    expect(out.startsWith(`${BEGIN}\n`)).toBe(true);
    expect(out.endsWith(`${END}\n${SENTRY_SCRIPT}`)).toBe(true);
    const local = out.indexOf('. "$SRCROOT/.xcode.env.local"');
    expect(out.indexOf('. "$SRCROOT/.xcode.env"')).toBeGreaterThan(-1);
    expect(local).toBeGreaterThan(out.indexOf('. "$SRCROOT/.xcode.env"'));
    expect(out.lastIndexOf('NODE_BINARY')).toBeGreaterThan(local);
  });

  it('keeps the disableAutoUpload export Sentry generated', () => {
    const out = applySentryNodeEnv(SENTRY_SCRIPT_NO_UPLOAD);
    expect(out.endsWith(`${END}\n${SENTRY_SCRIPT_NO_UPLOAD}`)).toBe(true);
  });

  it('is idempotent', () => {
    const once = applySentryNodeEnv(SENTRY_SCRIPT);
    expect(applySentryNodeEnv(once)).toBe(once);
    expect(once.split(BEGIN)).toHaveLength(2);
  });

  it('refreshes a stale generated block instead of duplicating it', () => {
    const stale = `${BEGIN}\nold_code_that_should_go_away\n${END}\n${SENTRY_SCRIPT}`;
    const out = applySentryNodeEnv(stale);
    expect(out).not.toContain('old_code_that_should_go_away');
    expect(out).toBe(applySentryNodeEnv(SENTRY_SCRIPT));
  });

  it('throws if the phase no longer calls sentry-xcode-debug-files.sh', () => {
    expect(() => applySentryNodeEnv('echo hi')).toThrow(/sentry-xcode-debug-files\.sh/);
  });

  it('rewrites a JSON-encoded existing phase (Sentry overwrite path)', () => {
    const proj = fakeProject(JSON.stringify(SENTRY_SCRIPT));
    applyToXcodeProject(proj);
    expect(JSON.parse(proj.phase.shellScript)).toBe(applySentryNodeEnv(SENTRY_SCRIPT));
    applyToXcodeProject(proj);
    expect(JSON.parse(proj.phase.shellScript)).toBe(applySentryNodeEnv(SENTRY_SCRIPT));
  });

  it('rewrites a raw-string phase (Sentry addBuildPhase path)', () => {
    const proj = fakeProject(SENTRY_SCRIPT);
    applyToXcodeProject(proj);
    expect(proj.phase.shellScript).toBe(applySentryNodeEnv(SENTRY_SCRIPT));
  });

  it('throws if the Sentry phase is missing', () => {
    const proj = { pbxItemByComment: () => undefined };
    expect(() => applyToXcodeProject(proj)).toThrow(/Upload Debug Symbols to Sentry/);
  });
});

describe('committed ios project', () => {
  // Guards against a hand edit or `expo prebuild` without the plugin: the
  // committed phase must equal the plugin's output for Sentry's script.
  it('has the plugin output in the "Upload Debug Symbols to Sentry" phase', () => {
    const script = committedPhaseScript();
    expect(script).toBe(applySentryNodeEnv(SENTRY_SCRIPT));
    const local = script.indexOf('.xcode.env.local');
    expect(local).toBeGreaterThan(-1);
    expect(script.indexOf('NODE_BINARY', local)).toBeGreaterThan(local);
  });

  it('registers the plugin before @sentry/react-native/expo in app.json', () => {
    // Expo runs the last-registered plugin's mod first, so ours must be listed
    // earlier to run after Sentry rewrites the phase.
    const { plugins } = require('../../app.json').expo;
    const names = plugins.map((p) => (Array.isArray(p) ? p[0] : p));
    const ours = names.indexOf('./plugins/withSentryDebugFilesNodeEnv');
    expect(ours).toBeGreaterThan(-1);
    expect(ours).toBeLessThan(names.indexOf('@sentry/react-native/expo'));
    expect(ours).toBeLessThan(names.indexOf('@sentry/react-native'));
  });
});

describe('phase script under /bin/sh (Xcode Cloud: no node on PATH)', () => {
  let dir;

  beforeEach(() => {
    dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sentry-phase-'));
    // SRCROOT (= frontend/ios) with the env files; PATH with no node on it;
    // a stub @sentry/react-native that node's require.resolve finds from cwd.
    fs.mkdirSync(path.join(dir, 'ios'));
    fs.mkdirSync(path.join(dir, 'emptybin'));
    const pkg = path.join(dir, 'node_modules/@sentry/react-native');
    fs.mkdirSync(path.join(pkg, 'scripts'), { recursive: true });
    fs.writeFileSync(path.join(pkg, 'package.json'), '{"name":"@sentry/react-native"}');
    fs.writeFileSync(
      path.join(pkg, 'scripts/sentry-xcode-debug-files.sh'),
      'echo "STUB_RAN NODE_BINARY=$NODE_BINARY"\n'
    );
  });

  afterEach(() => {
    fs.rmSync(dir, { recursive: true, force: true });
  });

  function run(script, extraEnv = {}) {
    return spawnSync('/bin/sh', ['-c', script], {
      cwd: dir,
      encoding: 'utf8',
      env: {
        PATH: path.join(dir, 'emptybin'),
        SRCROOT: path.join(dir, 'ios'),
        HOME: dir,
        ...extraEnv,
      },
    });
  }

  function writeEnv(name, body) {
    fs.writeFileSync(path.join(dir, 'ios', name), body);
  }

  it('reproduces the build 161 failure with the unpatched Sentry script', () => {
    writeEnv('.xcode.env.local', `export NODE_BINARY="${process.execPath}"\n`);
    const res = run(SENTRY_SCRIPT);
    expect(res.status).not.toBe(0);
    expect(res.stderr).toMatch(/node: (command )?not found/);
  });

  it('resolves Sentry via NODE_BINARY from .xcode.env.local', () => {
    // Mirrors ios/.xcode.env (`command -v node` is empty here) + the
    // .xcode.env.local that ci_post_clone.sh writes.
    writeEnv(
      '.xcode.env',
      'export NODE_BINARY=$(command -v node)\nexport SENTRY_ALLOW_FAILURE=true\n'
    );
    writeEnv('.xcode.env.local', `export NODE_BINARY="${process.execPath}"\n`);
    const res = run(committedPhaseScript());
    expect(res.stderr).not.toMatch(/not found/);
    expect(res.status).toBe(0);
    expect(res.stdout).toContain(`STUB_RAN NODE_BINARY=${process.execPath}`);
  });

  it('skips with a warning when Node cannot be found and SENTRY_ALLOW_FAILURE=true', () => {
    writeEnv(
      '.xcode.env',
      'export NODE_BINARY=$(command -v node)\nexport SENTRY_ALLOW_FAILURE=true\n'
    );
    const res = run(committedPhaseScript());
    expect(res.status).toBe(0);
    expect(res.stdout).toMatch(/^warning: Sentry dSYM upload skipped/m);
    expect(res.stdout).not.toContain('STUB_RAN');
  });

  it('fails with an error when Node cannot be found and failure is not allowed', () => {
    const res = run(committedPhaseScript());
    expect(res.status).toBe(1);
    expect(res.stdout).toMatch(/^error: Sentry dSYM upload/m);
  });
});
