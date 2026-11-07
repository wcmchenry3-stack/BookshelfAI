/**
 * #899 / #912 regression check: Android ignores `fontWeight` on a custom font
 * file, so text must pick its weight by font FILE (an AppText variant or
 * `fontFor()` from theme/typography.ts), never with `fontWeight`.
 *
 * 1. No object literal in app/ or components/ sets `fontWeight` together with
 *    `fontFamily` (the bug as filed; shorthand `{ fontWeight }` included),
 *    reported as file:line.
 * 2. `fontWeight` appears only in files still waiting to be migrated
 *    (MIGRATION_PENDING). Do not add a file to that list: migrate the text.
 *    The list may only shrink: a listed file without `fontWeight` fails, so
 *    the entry is removed in the PR that migrates it.
 * 3. Every font file the typography module names is loaded by useFonts in
 *    app/_layout.tsx.
 */
import fs from 'fs';
import path from 'path';

import { fontFiles } from '../../theme/typography';

const ROOT = path.join(__dirname, '../..');
const DIRS = ['app', 'components'];

// Files owned by other in-flight PRs of epic #897 at the time of #912; each one
// moves to variants in its own PR. The list may only shrink.
const MIGRATION_PENDING = new Set([
  'components/InAppBanner.tsx',
  'components/library/BookRow.tsx',
  'components/library/StatusTabs.tsx',
]);

/** A style key in an object body: `key: value`, or the shorthand `{ key }` / `{ key, ... }`. */
const hasKey = (body: string, key: string) =>
  new RegExp(`(?:^|[,{\\s])${key}\\s*(?::|,|$)`).test(body);

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(full));
    else if (/\.(ts|tsx)$/.test(entry.name)) out.push(full);
  }
  return out;
}

/** Source with comments blanked out (newlines kept so line numbers hold). */
function stripComments(src: string): string {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '))
    .replace(/(^|[^:'"`])\/\/.*$/gm, (m, p1: string) => p1 + ' '.repeat(m.length - p1.length));
}

const files = DIRS.flatMap((d) => walk(path.join(ROOT, d))).map((f) => ({
  rel: path.relative(ROOT, f).split(path.sep).join('/'),
  src: stripComments(fs.readFileSync(f, 'utf8')),
}));

describe('fontWeight is never combined with a custom font', () => {
  it('no style object sets fontWeight together with fontFamily', () => {
    const offenders: string[] = [];
    for (const { rel, src } of files) {
      // Innermost object literals: `name: { ... }` or `{ ... }` with no nested braces.
      const re = /\{([^{}]*)\}/g;
      let m: RegExpExecArray | null;
      while ((m = re.exec(src))) {
        const body = m[1].trim();
        if (hasKey(body, 'fontWeight') && hasKey(body, 'fontFamily')) {
          const line = src.slice(0, m.index).split('\n').length;
          offenders.push(`${rel}:${line}`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it('the same-object rule also catches the shorthand form', () => {
    expect(hasKey('fontWeight, fontFamily', 'fontWeight')).toBe(true);
    expect(hasKey('fontFamily, fontWeight', 'fontWeight')).toBe(true);
    expect(hasKey("fontWeight: '700'", 'fontWeight')).toBe(true);
    expect(hasKey('fontFamily: theme.typography.fontWeightBold', 'fontWeight')).toBe(false);
  });

  it('fontWeight appears only in files still pending migration', () => {
    const withWeight = files
      .filter(({ src }) => /\bfontWeight\b/.test(src))
      .map(({ rel }) => rel)
      .filter((rel) => !MIGRATION_PENDING.has(rel));
    expect(withWeight).toEqual([]);
  });

  it('the pending list only shrinks: every listed file still has fontWeight', () => {
    const migrated = [...MIGRATION_PENDING].filter((rel) => {
      const f = files.find((x) => x.rel === rel);
      return !f || !/\bfontWeight\b/.test(f.src);
    });
    // Remove these from MIGRATION_PENDING: they no longer use fontWeight.
    expect(migrated).toEqual([]);
  });

  it('every typography font file is loaded by useFonts in app/_layout.tsx', () => {
    const layout = fs.readFileSync(path.join(ROOT, 'app/_layout.tsx'), 'utf8');
    const block = layout.match(/useFonts\(\{([\s\S]*?)\}\)/);
    expect(block).not.toBeNull();
    const loaded = new Set(block![1].split(/[\s,]+/).filter(Boolean));
    const needed = [...Object.values(fontFiles.sans), ...Object.values(fontFiles.serif)];
    for (const f of needed) expect(loaded).toContain(f);
    // Nothing loaded that no style uses (#899: unused font files are not loaded).
    expect([...loaded].sort()).toEqual([...needed].sort());
  });
});
