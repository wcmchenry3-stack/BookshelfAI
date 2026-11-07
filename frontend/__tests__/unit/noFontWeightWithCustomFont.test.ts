/**
 * #899 / #912 regression check: Android ignores `fontWeight` on a custom font
 * file, so text must pick its weight by font FILE (an AppText variant or
 * `fontFor()` from theme/typography.ts), never with `fontWeight`.
 *
 * 1. No object literal in app/ or components/ sets `fontWeight` together with
 *    `fontFamily` (the bug as filed), reported as file:line.
 * 2. `fontWeight` appears only in files still waiting to be migrated
 *    (MIGRATION_PENDING). Do not add a file to that list: migrate the text.
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
  'app/_layout.tsx', // header wordmark (header section owned by the app-shell PR)
  'components/InAppBanner.tsx',
  'components/library/BookRow.tsx',
  'components/library/StatusTabs.tsx',
]);

// Same-object fontWeight + fontFamily still present, with the follow-up owner.
const SAME_OBJECT_PENDING = new Set(['app/_layout.tsx:wordmark']);

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
    const seenPending = new Set<string>();
    for (const { rel, src } of files) {
      // Innermost object literals: `name: { ... }` or `{ ... }` with no nested braces.
      const re = /(?:(\w+)\s*:\s*)?\{([^{}]*)\}/g;
      let m: RegExpExecArray | null;
      while ((m = re.exec(src))) {
        const body = m[2];
        if (/\bfontWeight\s*:/.test(body) && /\bfontFamily\s*:/.test(body)) {
          const key = `${rel}:${m[1] ?? ''}`;
          if (SAME_OBJECT_PENDING.has(key)) {
            seenPending.add(key);
            continue;
          }
          const line = src.slice(0, m.index).split('\n').length;
          offenders.push(`${rel}:${line}`);
        }
      }
    }
    expect(offenders).toEqual([]);
    // A pending entry that has been fixed must be removed from the list.
    expect([...seenPending].sort()).toEqual([...SAME_OBJECT_PENDING].sort());
  });

  it('fontWeight appears only in files still pending migration', () => {
    const withWeight = files
      .filter(({ src }) => /\bfontWeight\s*:/.test(src))
      .map(({ rel }) => rel)
      .filter((rel) => !MIGRATION_PENDING.has(rel));
    expect(withWeight).toEqual([]);
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
