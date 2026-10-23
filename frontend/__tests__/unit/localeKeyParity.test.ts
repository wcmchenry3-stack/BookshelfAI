/**
 * Every key in the English locale must exist in every other locale, so
 * i18next never silently falls back to English (#609).
 *
 * Plural keys: English writes `x_one` / `x_other`. A locale satisfies `x` when
 * it has `x_other` (every CLDR language has an `other` category). A locale may
 * also carry its own categories (`x_zero`, `x_two`, `x_few`, `x_many`, ...).
 * Any other key that English does not have is stale and fails the test.
 */
import fs from 'fs';
import path from 'path';

const LOCALES_DIR = path.join(__dirname, '../../src/i18n/locales');

type Json = { [k: string]: Json | string };

function flatten(obj: Json, prefix = ''): string[] {
  const out: string[] = [];
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (typeof v === 'string') out.push(key);
    else out.push(...flatten(v, key));
  }
  return out;
}

const PLURAL_SUFFIX = /_(zero|one|two|few|many|other)$/;
const EN_PLURAL_SUFFIX = /_(one|other)$/;

const read = (locale: string, ns: string): Json =>
  JSON.parse(fs.readFileSync(path.join(LOCALES_DIR, locale, `${ns}.json`), 'utf8'));

const namespaces = fs
  .readdirSync(path.join(LOCALES_DIR, 'en'))
  .filter((f) => f.endsWith('.json'))
  .map((f) => f.replace('.json', ''));
const locales = fs
  .readdirSync(LOCALES_DIR, { withFileTypes: true })
  .filter((d) => d.isDirectory() && d.name !== 'en' && d.name !== '_meta')
  .map((d) => d.name);

describe('locale key parity with en', () => {
  it('finds the locales and namespaces', () => {
    expect(locales.length).toBeGreaterThanOrEqual(12);
    expect(namespaces.length).toBeGreaterThanOrEqual(9);
  });

  describe.each(locales)('%s', (locale) => {
    it.each(namespaces)('%s has every en key and no stale keys', (ns) => {
      const enKeys = flatten(read('en', ns));
      const enPluralBases = new Set(
        enKeys.filter((k) => EN_PLURAL_SUFFIX.test(k)).map((k) => k.replace(EN_PLURAL_SUFFIX, ''))
      );
      const locKeys = new Set(flatten(read(locale, ns)));

      const missing = enKeys
        .filter((k) => {
          if (!EN_PLURAL_SUFFIX.test(k)) return !locKeys.has(k);
          // x_one is satisfied by x_one or by the locale's own set (x_other required)
          return !locKeys.has(`${k.replace(EN_PLURAL_SUFFIX, '')}_other`);
        })
        .map((k) => `${ns}:${k}`);
      const enSet = new Set(enKeys);
      const stale = [...locKeys]
        .filter(
          (k) =>
            !enSet.has(k) &&
            !(PLURAL_SUFFIX.test(k) && enPluralBases.has(k.replace(PLURAL_SUFFIX, '')))
        )
        .map((k) => `${ns}:${k}`);

      expect({ missing: [...new Set(missing)], stale }).toEqual({ missing: [], stale: [] });
    });
  });
});
