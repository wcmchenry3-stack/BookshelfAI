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
 * What it matches (inside string/template literals only, comments ignored):
 *   - rgb(/rgba(/hsl(/hsla( anywhere in the string, e.g. '0 0 4px rgba(0,0,0,.2)'
 *   - '#' + 3/4/6/8 hex digits anywhere, e.g. '1px solid #fff', 'linear-gradient(#fff,#000)'
 * Not detected: named colours ('white', 'red'), 'transparent', and colours built
 * at runtime. Strings containing '://' (URLs with #anchors) are skipped. An id
 * like '#123' (or '(#632)') still matches as hex, which is accepted.
 *
 * No allowlist or baseline. If an entry is ever truly unavoidable, add it to
 * ALLOWED below with a reason.
 */
import fs from 'fs';
import path from 'path';

const ROOT = path.join(__dirname, '../..');
const DIRS = ['app', 'components', 'contexts', 'hooks', 'lib'];
const ALLOWED: { file: string; line: number; reason: string }[] = [];

const HEX = /(?<![\w&-])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![\w-])/;
const FUNC = /\b(?:rgba?|hsla?)\(/i;

function isColourString(body: string): boolean {
  if (body.includes('://')) return false;
  return HEX.test(body) || FUNC.test(body);
}

// Returns 1-based line numbers holding a colour literal. Comments are stripped
// (string-aware, so '//' inside a URL string is kept); block comments may span lines.
function findLiterals(source: string): number[] {
  const hits: number[] = [];
  let inBlock = false;
  source.split('\n').forEach((raw, i) => {
    let quote: string | null = null;
    let body = '';
    let hit = false;
    for (let j = 0; j < raw.length; j++) {
      const ch = raw[j];
      const next = raw[j + 1];
      if (inBlock) {
        if (ch === '*' && next === '/') {
          inBlock = false;
          j++;
        }
      } else if (quote) {
        if (ch === '\\') {
          body += ch + (next ?? '');
          j++;
        } else if (ch === quote) {
          if (isColourString(body)) hit = true;
          quote = null;
        } else {
          body += ch;
        }
      } else if (ch === '/' && next === '*') {
        inBlock = true;
        j++;
      } else if (ch === '/' && next === '/') {
        break;
      } else if (ch === "'" || ch === '"' || ch === '`') {
        quote = ch;
        body = '';
      }
    }
    if (quote && isColourString(body)) hit = true; // multi-line template tail
    if (hit) hits.push(i + 1);
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
    expect(findLiterals(`s: '0 0 4px rgba(0,0,0,.2)'`)).toEqual([1]);
    expect(findLiterals(`g: 'linear-gradient(#fff,#000)'`)).toEqual([1]);
    expect(findLiterals(`b: '1px solid #fff'`)).toEqual([1]);
    expect(findLiterals(`/* x */ const c = '#fff';`)).toEqual([1]);
  });

  it('detector ignores comments, anchors, URLs and non-colour hashes', () => {
    expect(findLiterals(`// fixes (#632) '#fff'`)).toEqual([]);
    expect(findLiterals(`/* a\n '#fff'\n*/ const x = 1;`)).toEqual([]);
    expect(findLiterals(`t('a#fff'); '#section'; 'see #12'; '#abcde'; 'pre-#174 state'`)).toEqual(
      []
    );
    expect(findLiterals(`u = 'https://x.com/p#fade'`)).toEqual([]);
    expect(findLiterals(`const url = 'https://x.com'; // '#fff'`)).toEqual([]);
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
