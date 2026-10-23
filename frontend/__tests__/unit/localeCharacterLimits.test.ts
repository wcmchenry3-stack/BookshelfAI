/**
 * Every string with a `characterLimit` in a namespace's `_meta` file must fit
 * that limit in every locale (the banner copy is shown in two lines on a
 * 375 pt phone, so the limit is a layout contract, not a suggestion).
 */
import fs from 'fs';
import path from 'path';

const LOCALES_DIR = path.join(__dirname, '../../src/i18n/locales');
const META_DIR = path.join(LOCALES_DIR, '_meta');

type Json = { [k: string]: Json | string };

function flatten(obj: Json, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (typeof v === 'string') out[key] = v;
    else Object.assign(out, flatten(v, key));
  }
  return out;
}

const read = (p: string): Json => JSON.parse(fs.readFileSync(p, 'utf8'));
const locales = fs
  .readdirSync(LOCALES_DIR, { withFileTypes: true })
  .filter((d) => d.isDirectory() && d.name !== '_meta')
  .map((d) => d.name);
const namespaces = fs.readdirSync(META_DIR).map((f) => f.replace('.meta.json', ''));

// Debt list for strings that exceed their limit. It is intentionally empty:
// shorten the string (do not raise the limit). A stale entry here fails the test.
const KNOWN_OVERRUNS = new Set<string>([]);

describe('locale strings respect their _meta characterLimit', () => {
  it.each(namespaces)('%s', (ns) => {
    const limits: Record<string, number> = {};
    for (const [k, v] of Object.entries(read(path.join(META_DIR, `${ns}.meta.json`)))) {
      const limit = (v as unknown as { characterLimit?: unknown }).characterLimit;
      if (typeof limit === 'number') limits[k] = limit;
    }
    const tooLong: string[] = [];
    const seen = new Set<string>();
    for (const locale of locales) {
      const file = path.join(LOCALES_DIR, locale, `${ns}.json`);
      if (!fs.existsSync(file)) continue;
      const strings = flatten(read(file));
      for (const [key, limit] of Object.entries(limits)) {
        const value = strings[key];
        if (typeof value === 'string' && [...value].length > limit) {
          const id = `${locale}/${ns}.${key}`;
          seen.add(id);
          if (!KNOWN_OVERRUNS.has(id)) tooLong.push(`${id}: ${[...value].length} > ${limit}`);
        }
      }
    }
    expect(tooLong).toEqual([]);
    const stale = [...KNOWN_OVERRUNS].filter(
      (id) => id.split('/')[1].startsWith(`${ns}.`) && !seen.has(id)
    );
    expect(stale).toEqual([]);
  });

  it('the scan namespace has limits to enforce', () => {
    const meta = read(path.join(META_DIR, 'scan.meta.json'));
    const n = Object.values(meta).filter(
      (v) => typeof (v as unknown as { characterLimit?: unknown }).characterLimit === 'number'
    ).length;
    expect(n).toBeGreaterThan(10);
  });
});
