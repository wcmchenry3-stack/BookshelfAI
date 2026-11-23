/** #685: the If-Match config and the 412 check. */
import { ifMatch, isVersionConflict, VERSION_MISMATCH } from '../../lib/entryVersion';

describe('ifMatch', () => {
  it('quotes the version as a strong ETag', () => {
    expect(ifMatch(1)).toEqual([{ headers: { 'If-Match': '"1"' } }]);
    expect(ifMatch(42)).toEqual([{ headers: { 'If-Match': '"42"' } }]);
  });

  it.each([undefined, Number.NaN, 1.5])('sends nothing for %p', (version) => {
    expect(ifMatch(version)).toEqual([]);
  });
});

describe('isVersionConflict', () => {
  const failure = (status: number, detail: unknown) => ({
    response: { status, data: { detail }, headers: {} },
  });

  it('is true for 412 version_mismatch', () => {
    expect(isVersionConflict(failure(412, { code: VERSION_MISMATCH, current_version: 2 }))).toBe(
      true
    );
  });

  it.each([
    [412, { code: 'other' }],
    [409, { code: VERSION_MISMATCH }],
    [400, 'invalid_if_match'],
  ])('is false for %s %p', (status, detail) => {
    expect(isVersionConflict(failure(status, detail))).toBe(false);
  });

  it('is false for a network error', () => {
    expect(isVersionConflict(new Error('Network Error'))).toBe(false);
  });
});
