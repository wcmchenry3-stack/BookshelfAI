/**
 * Repo-wide Hard Rule 5 check: no colour literals in app code.
 *
 * Walks frontend/{app,components,contexts,hooks,lib} (theme/ and tests are not
 * scanned) and fails on any string literal that is a hex colour or an
 * rgb()/rgba()/hsl()/hsla() call, reporting file:line. It replaces the
 * InAppBanner-only check. Colours belong in theme/tokens.ts.
 *
 * Relationship to CI: the shared `called-design-token-check.yml` workflow
 * (wcmchenry3-stack/.github, invoked from .github/workflows/design-token-check.yml)
 * is the org-level design-token/accessibility gate and is not visible from this
 * repo. This test is the in-repo, per-PR Jest guard and does not try to
 * reproduce that workflow.
 *
 * No allowlist or baseline. If an entry is ever truly unavoidable, add it to
 * ALLOWED below with a reason.
 */
import fs from 'fs';
import path from 'path';

const ROOT = path.join(__dirname, '../..');
const DIRS = ['app', 'components', 'contexts', 'hooks', 'lib'];
const ALLOWED: { file: string; line: number; reason: string }[] = [];

// Hex only inside a string literal (so "#632" in comments or i18n keys like
// 'a#b' are not hits); colour functions only at the start of a string literal.
const HEX = /(['"`])#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})\1/;
const FUNC = /(['"`])\s*(?:rgba?|hsla?)\(/;

function findLiterals(source: string): number[] {
  const hits: number[] = [];
  source.split('\n').forEach((raw, i) => {
    const line = raw.trim();
    if (line.startsWith('//') || line.startsWith('*') || line.startsWith('/*')) return;
    if (HEX.test(raw) || FUNC.test(raw)) hits.push(i + 1);
  });
  return hits;
}

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === '__tests__' || entry.name === 'node_modules') continue;
      walk(full, out);
    } else if (/\.(ts|tsx)$/.test(entry.name) && !/\.(test|spec)\.tsx?$/.test(entry.name)) {
      out.push(full);
    }
  }
  return out;
}

describe('no colour literals (Hard Rule 5)', () => {
  it('detector flags colours and ignores look-alikes', () => {
    expect(findLiterals(`const a = '#fff';`)).toEqual([1]);
    expect(findLiterals(`x\nconst a = "#007AFF";`)).toEqual([2]);
    expect(findLiterals('c: `rgba(0,0,0,0.5)`')).toEqual([1]);
    expect(findLiterals(`c: 'hsl(10, 50%, 50%)'`)).toEqual([1]);
    expect(findLiterals(`// fixes (#632)\nt('a#b'); url('https://x/#top'); '#abcde'`)).toEqual([]);
  });

  it('finds source files to scan', () => {
    const files = DIRS.flatMap((d) => walk(path.join(ROOT, d)));
    expect(files.length).toBeGreaterThan(50);
  });

  it('app code contains no hex/rgb()/rgba()/hsl() string literals', () => {
    const offenders: string[] = [];
    for (const d of DIRS) {
      for (const file of walk(path.join(ROOT, d))) {
        const rel = path.relative(ROOT, file);
        const src = fs.readFileSync(file, 'utf8');
        for (const line of findLiterals(src)) {
          if (!ALLOWED.some((a) => a.file === rel && a.line === line)) {
            offenders.push(`${rel}:${line}`);
          }
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
