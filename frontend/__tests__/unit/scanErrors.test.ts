import {
  classifyScanError,
  formatPeriodEnd,
  nextUtcMidnight,
  scanNoticeMessage,
  toScanNotice,
  type ScanUiState,
} from '../../lib/scanErrors';

function err(status: number, detail?: string, extra: Record<string, unknown> = {}, headers = {}) {
  return { response: { status, data: detail ? { detail, ...extra } : {}, headers } };
}

describe('classifyScanError: every backend code maps to exactly one UI state', () => {
  const cases: [string, unknown, ScanUiState][] = [
    [
      '402 scan_limit_reached',
      err(402, 'scan_limit_reached', { limit: 5, used: 5, period_end: '2026-11-01' }),
      { kind: 'monthly_limit', limit: 5, periodEnd: '2026-11-01' },
    ],
    ['402 no_enhanced_credits', err(402, 'no_enhanced_credits'), { kind: 'no_enhanced_credits' }],
    ['402 with no detail (legacy)', err(402), { kind: 'no_enhanced_credits' }],
    ['402 unknown detail', err(402, 'something_else'), { kind: 'generic' }],
    [
      '429 daily_scan_limit',
      err(429, 'daily_scan_limit', {}, { 'retry-after': '3600' }),
      { kind: 'daily_limit', retryAfterSeconds: 3600 },
    ],
    [
      '429 scan_in_progress',
      err(429, 'scan_in_progress', {}, { 'retry-after': '2' }),
      { kind: 'in_progress', retryAfterSeconds: 2 },
    ],
    ['429 unknown detail', err(429, 'rate_limited'), { kind: 'generic' }],
    ['503 scan_disabled', err(503, 'scan_disabled'), { kind: 'paused' }],
    [
      '503 enhanced_scan_disabled',
      err(503, 'enhanced_scan_disabled'),
      { kind: 'paused', enhancedOnly: true },
    ],
    ['503 scan_capacity', err(503, 'scan_capacity'), { kind: 'paused' }],
    ['503 ai_unavailable', err(503, 'ai_unavailable'), { kind: 'paused' }],
    ['503 scan_unavailable', err(503, 'scan_unavailable'), { kind: 'paused' }],
    ['503 unknown detail', err(503, 'mystery'), { kind: 'generic' }],
    ['500', err(500, 'boom'), { kind: 'generic' }],
    ['no response (network)', { message: 'Network Error' }, { kind: 'generic' }],
    ['null', null, { kind: 'generic' }],
  ];

  it.each(cases)('%s', (_name, error, expected) => {
    expect(classifyScanError(error)).toEqual(expected);
  });

  it('reads Retry-After from an AxiosHeaders-like object', () => {
    const headers = { get: (n: string) => (n.toLowerCase() === 'retry-after' ? '4' : undefined) };
    expect(classifyScanError(err(429, 'scan_in_progress', {}, headers))).toEqual({
      kind: 'in_progress',
      retryAfterSeconds: 4,
    });
  });

  it('defaults and clamps the in-progress wait', () => {
    expect(classifyScanError(err(429, 'scan_in_progress'))).toEqual({
      kind: 'in_progress',
      retryAfterSeconds: 2,
    });
    expect(classifyScanError(err(429, 'scan_in_progress', {}, { 'retry-after': '999' }))).toEqual({
      kind: 'in_progress',
      retryAfterSeconds: 10,
    });
  });
});

describe('toScanNotice', () => {
  const now = Date.UTC(2026, 9, 22, 15, 30);

  it('daily limit resets at Retry-After, else the next UTC midnight', () => {
    expect(toScanNotice({ kind: 'daily_limit', retryAfterSeconds: 60 }, now)).toEqual({
      kind: 'daily_limit',
      resetsAt: now + 60_000,
    });
    expect(toScanNotice({ kind: 'daily_limit' }, now)).toEqual({
      kind: 'daily_limit',
      resetsAt: Date.UTC(2026, 9, 23),
    });
    expect(nextUtcMidnight(now)).toBe(Date.UTC(2026, 9, 23));
  });

  it('in_progress only becomes a notice once retries are exhausted', () => {
    const state: ScanUiState = { kind: 'in_progress', retryAfterSeconds: 2 };
    expect(toScanNotice(state, now)).toBeNull();
    expect(toScanNotice(state, now, true)).toEqual({ kind: 'still_processing' });
  });

  it('has no notice for generic or credits', () => {
    expect(toScanNotice({ kind: 'generic' })).toBeNull();
    expect(toScanNotice({ kind: 'no_enhanced_credits' })).toBeNull();
  });
});

describe('copy', () => {
  // Minimal t() so this stays a pure unit test of which key + params are used.
  const t = (key: string, o?: Record<string, unknown>) => `${key}:${JSON.stringify(o ?? {})}`;

  it('formats the reset date as a UTC calendar date', () => {
    expect(formatPeriodEnd('2026-11-01', 'en')).toBe('November 1, 2026');
    expect(formatPeriodEnd('garbage', 'en')).toBeNull();
    expect(formatPeriodEnd(undefined, 'en')).toBeNull();
  });

  it('monthly limit uses the limit and the reset date', () => {
    const msg = scanNoticeMessage(
      { kind: 'monthly_limit', limit: 5, periodEnd: '2026-11-01' },
      t,
      'en'
    );
    expect(msg).toContain('monthlyLimit:{"count":5}');
    expect(msg).toContain('monthlyResets:{"date":"November 1, 2026"}');
  });

  it('monthly limit without numbers falls back to the generic line', () => {
    expect(scanNoticeMessage({ kind: 'monthly_limit' }, t, 'en')).toBe('monthlyLimitAny:{}');
  });
});
