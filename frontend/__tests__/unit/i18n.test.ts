/**
 * i18n infrastructure tests.
 *
 * Architecture: en is bundled statically (always synchronous), all other
 * locales are lazy-loaded via i18next-resources-to-backend using the importMap.
 *
 * Validates that:
 * - importMap covers all 10 non-en active locales with 8 namespaces each
 * - en JSON files have the correct keys and values (via direct require)
 * - No English key has __NEEDS_TRANSLATION__ as its value, and no locale file
 *   contains it at all
 * - Exactly 11 locales are active (ar/he excluded until RTL ready)
 * - RTL_LOCALES is empty
 */

import fs from 'fs';
import path from 'path';

import { importMap } from '../../src/i18n/i18n';
import { LOCALES, RTL_LOCALES } from '../../src/i18n/locales';

const NAMESPACES = [
  'common',
  'auth',
  'tabs',
  'my-books',
  'scan',
  'settings',
  'wishlist',
  'components',
] as const;

const NON_EN_LOCALES = LOCALES.filter((l) => l.code !== 'en').map((l) => l.code);

describe('importMap structure', () => {
  it('covers all 10 non-en active locales', () => {
    expect(Object.keys(importMap).length).toBe(NON_EN_LOCALES.length);
    for (const code of NON_EN_LOCALES) {
      expect(importMap[code]).toBeDefined();
    }
  });

  it('has all 8 namespaces for every non-en locale', () => {
    for (const code of NON_EN_LOCALES) {
      for (const ns of NAMESPACES) {
        expect(typeof importMap[code]?.[ns]).toBe('function');
      }
    }
  });

  it('does not include inactive RTL locales (ar, he)', () => {
    expect(importMap['ar']).toBeUndefined();
    expect(importMap['he']).toBeUndefined();
  });

  it('does not include en (en is statically bundled)', () => {
    expect(importMap['en']).toBeUndefined();
  });
});

describe('en namespace content', () => {
  it.each(NAMESPACES)('en/%s has at least one key', (ns) => {
    const data = require(`../../src/i18n/locales/en/${ns}.json`);
    expect(Object.keys(data).length).toBeGreaterThan(0);
  });

  it('en/auth.appTitle is "BookshelfAI"', () => {
    const data = require('../../src/i18n/locales/en/auth.json');
    expect(data.appTitle).toBe('BookshelfAI');
  });

  it('en/tabs has all 4 tab keys', () => {
    const data = require('../../src/i18n/locales/en/tabs.json');
    expect(data.scan).toBeDefined();
    expect(data.wishlist).toBeDefined();
    expect(data.myBooks).toBeDefined();
    expect(data.settings).toBeDefined();
  });

  it('en/my-books has all status tab keys', () => {
    const data = require('../../src/i18n/locales/en/my-books.json');
    expect(data.statusTab?.all).toBeDefined();
    expect(data.statusTab?.wishlisted).toBeDefined();
    expect(data.statusTab?.purchased).toBeDefined();
    expect(data.statusTab?.reading).toBeDefined();
    expect(data.statusTab?.read).toBeDefined();
  });

  it('no English key has __NEEDS_TRANSLATION__ as value', () => {
    const problematic: string[] = [];

    function checkValues(obj: unknown, path: string) {
      if (typeof obj === 'string') {
        if (obj === '__NEEDS_TRANSLATION__') problematic.push(path);
      } else if (typeof obj === 'object' && obj !== null) {
        for (const [k, v] of Object.entries(obj)) {
          checkValues(v, `${path}.${k}`);
        }
      }
    }

    for (const ns of NAMESPACES) {
      const data = require(`../../src/i18n/locales/en/${ns}.json`);
      checkValues(data, `en.${ns}`);
    }

    expect(problematic).toEqual([]);
  });

  // The translate script writes __NEEDS_TRANSLATION__ when it has no
  // translation, and nothing falls back at runtime, so users would see it.
  it('no locale, active or not, ships an untranslated placeholder', () => {
    const dir = path.join(__dirname, '../../src/i18n/locales');
    const problematic: string[] = [];
    for (const code of fs.readdirSync(dir)) {
      if (code.startsWith('_') || !fs.statSync(path.join(dir, code)).isDirectory()) continue;
      for (const file of fs.readdirSync(path.join(dir, code))) {
        if (!file.endsWith('.json')) continue;
        const raw = fs.readFileSync(path.join(dir, code, file), 'utf8');
        if (raw.includes('__NEEDS_TRANSLATION__')) problematic.push(`${code}/${file}`);
      }
    }
    expect(problematic).toEqual([]);
  });
});

describe('i18n locale config', () => {
  it('registers exactly 11 active locales', () => {
    expect(LOCALES.length).toBe(11);
  });
});

describe('RTL_LOCALES', () => {
  it('is empty — ar and he are inactive until RTL layout is implemented', () => {
    expect(RTL_LOCALES.size).toBe(0);
  });

  it('does not contain any ltr locales', () => {
    expect(RTL_LOCALES.has('en')).toBe(false);
    expect(RTL_LOCALES.has('es')).toBe(false);
    expect(RTL_LOCALES.has('fr-CA')).toBe(false);
  });
});
