/**
 * Regression tests for #779 (CVE-2026-118664, decode-uri-component < 0.5.1).
 *
 * expo-router -> query-string@7 require()s decode-uri-component. The fixed
 * upstream release is ESM-only, so the repo vendors a CommonJS port
 * (frontend/vendor/decode-uri-component, wired in by an npm override). These
 * tests go through query-string exactly as the app resolves it, so they fail if
 * the override is dropped (vulnerable 0.2.2: quadratic hang) or if a plain ESM
 * override returns a namespace object (decodeComponent is not a function).
 */
import * as queryString from 'query-string';

// eslint-disable-next-line @typescript-eslint/no-require-imports
const decodeUriComponent = require('decode-uri-component');
// eslint-disable-next-line @typescript-eslint/no-require-imports
const pkg = require('decode-uri-component/package.json');

describe('decode-uri-component resolution', () => {
  it('resolves to the vendored fixed build, callable via require()', () => {
    expect(typeof decodeUriComponent).toBe('function');
    expect(pkg.version).toMatch(/^0\.5\.1\+bookshelf\./);
  });

  it('rejects non-strings like upstream 0.5.1', () => {
    expect(() => decodeUriComponent(1)).toThrow(TypeError);
  });
});

describe('query-string parse', () => {
  it('decodes %20 and a multi-byte sequence', () => {
    expect(queryString.parse('a=hello%20world&b=%E2%9C%93')).toEqual({
      a: 'hello world',
      b: '✓',
    });
  });

  it('does not throw on a malformed sequence (%E0%A4%A)', () => {
    let result: unknown;
    expect(() => {
      result = queryString.parse('x=%E0%A4%A');
    }).not.toThrow();
    expect(result).toEqual({ x: '%E0%A4%A' });
  });

  it('decodes a valid run mixed with a malformed tail', () => {
    expect(queryString.parse('x=%E2%9C%93%E0%A4%A')).toEqual({ x: '✓%E0%A4%A' });
  });

  it('finishes quickly on a 100k-char adversarial percent-encoded run', () => {
    const run = '%E0%A4%A'.repeat(12500); // 100k chars, invalid so the slow path runs
    expect(run.length).toBe(100000);
    const start = Date.now();
    const result = queryString.parse(`x=${run}`);
    const elapsed = Date.now() - start;
    expect(typeof result.x).toBe('string');
    expect(elapsed).toBeLessThan(2000);
  });
});

// Inputs that separate 0.2.2 from the fix: on 0.2.2 the first takes ~10 s and
// then throws RangeError (stack overflow); the fixed build takes milliseconds.
const ADVERSARIAL: [string, string, string][] = [
  ['%41 run + malformed tail', '%41'.repeat(30000) + '%E0%A4%A', 'A'.repeat(30000) + '%E0%A4%A'],
  ['%80 run + bare %', '%80'.repeat(30000) + '%', '%80'.repeat(30000) + '%'],
  ['%C0%80 run + bare %', '%C0%80'.repeat(15000) + '%', '%C0%80'.repeat(15000) + '%'],
];

describe.each(ADVERSARIAL)('adversarial input: %s', (_name, input, expected) => {
  it('decodeUriComponent returns the expected string without throwing', () => {
    const start = Date.now();
    expect(decodeUriComponent(input)).toBe(expected);
    expect(Date.now() - start).toBeLessThan(5000); // backstop only
  });

  it('queryString.parse returns the expected string without throwing', () => {
    const start = Date.now();
    expect(queryString.parse('q=' + input).q).toBe(expected);
    expect(Date.now() - start).toBeLessThan(5000); // backstop only
  });
});

describe('query-string parseUrl', () => {
  it('decodes the query and keeps the url', () => {
    expect(queryString.parseUrl('https://example.com/p?a=hello%20world&b=%E2%9C%93')).toEqual({
      url: 'https://example.com/p',
      query: { a: 'hello world', b: '✓' },
    });
  });

  it('does not throw on a malformed sequence', () => {
    expect(queryString.parseUrl('/p?x=%E0%A4%A').query).toEqual({ x: '%E0%A4%A' });
  });

  it('finishes quickly on an adversarial run', () => {
    const start = Date.now();
    const parsed = queryString.parseUrl(`/p?x=${'%E0%A4%A'.repeat(12500)}`);
    expect(typeof parsed.query.x).toBe('string');
    expect(Date.now() - start).toBeLessThan(2000);
  });
});
