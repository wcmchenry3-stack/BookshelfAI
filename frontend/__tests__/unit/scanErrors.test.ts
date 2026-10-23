import {
  classifyScanError,
  hasRefusedJobs,
  enhancedRefusalMessage,
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
    // #582: no monthly limit. A 402 from an older server (monthly or
    // enhanced monthly allowance) is a generic failure, never a limit notice.
    [
      '402 scan_limit_reached (older server)',
      err(402, 'scan_limit_reached', { limit: 5, used: 5, period_end: '2026-11-01' }),
      { kind: 'generic' },
    ],
    [
      '402 enhanced_limit_reached (older server)',
      err(402, 'enhanced_limit_reached', { limit: 20, used: 20, period_end: '2026-11-01' }),
      { kind: 'generic' },
    ],
    // #533: enhanced refusals replace 402 no_enhanced_credits (gone, as is
    // the bare-402 meaning "out of credits").
    [
      '403 enhanced_requires_premium',
      err(403, 'enhanced_requires_premium'),
      { kind: 'enhanced_refused', reason: 'premium_required' },
    ],
    [
      '429 enhanced_throttled',
      err(429, 'enhanced_throttled', {}, { 'retry-after': '5400' }),
      { kind: 'enhanced_refused', reason: 'throttled', retryAfterSeconds: 5400 },
    ],
    [
      '429 enhanced_throttled without Retry-After',
      err(429, 'enhanced_throttled'),
      { kind: 'enhanced_refused', reason: 'throttled', retryAfterSeconds: undefined },
    ],
    ['402 no_enhanced_credits (removed)', err(402, 'no_enhanced_credits'), { kind: 'generic' }],
    ['402 with no detail', err(402), { kind: 'generic' }],
    ['403 other detail', err(403, 'Turnstile verification failed'), { kind: 'generic' }],
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
    ['503 ai_budget_reached', err(503, 'ai_budget_reached'), { kind: 'paused' }],
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

  it('has no notice for generic or enhanced refusals (a banner speaks)', () => {
    expect(toScanNotice({ kind: 'generic' })).toBeNull();
    expect(toScanNotice({ kind: 'enhanced_refused', reason: 'premium_required' })).toBeNull();
    expect(
      toScanNotice({ kind: 'enhanced_refused', reason: 'throttled', retryAfterSeconds: 60 })
    ).toBeNull();
  });
});

describe('copy', () => {
  // Minimal t() so this stays a pure unit test of which key + params are used.
  const t = (key: string, o?: Record<string, unknown>) => `${key}:${JSON.stringify(o ?? {})}`;

  it('every notice has its own line, none monthly (#582)', () => {
    expect(scanNoticeMessage({ kind: 'daily_limit', resetsAt: 0 }, t, 'en')).toMatch(
      /^dailyLimit:/
    );
    expect(scanNoticeMessage({ kind: 'still_processing' }, t, 'en')).toBe('stillProcessing:{}');
    expect(scanNoticeMessage({ kind: 'paused' }, t, 'en')).toBe('scanPaused:{}');
  });

  it('enhanced refusals each have their own line', () => {
    expect(enhancedRefusalMessage({ reason: 'premium_required' }, t, 'en')).toBe(
      'enhancedRequiresPremium:{}'
    );
    expect(enhancedRefusalMessage({ reason: 'throttled' }, t, 'en')).toBe(
      'enhancedThrottledLater:{}'
    );
  });

  it('a throttle names the local time from Retry-After', () => {
    const now = Date.UTC(2026, 9, 22, 15, 30);
    const msg = enhancedRefusalMessage(
      { reason: 'throttled', retryAfterSeconds: 3600 },
      t,
      'en',
      now
    );
    const expected = new Date(now + 3_600_000).toLocaleTimeString('en', {
      hour: 'numeric',
      minute: '2-digit',
    });
    expect(msg).toBe(`enhancedThrottled:${JSON.stringify({ time: expected })}`);
  });
});

describe('hasRefusedJobs', () => {
  it('counts a queued job refused for a daily limit, pause or scan in progress', () => {
    for (const error of ['daily_limit', 'paused', 'in_progress']) {
      expect(hasRefusedJobs([{ status: 'queued', error }])).toBe(true);
    }
  });

  it('still counts a job persisted by an older build with monthly_limit (#582, one release)', () => {
    expect(hasRefusedJobs([{ status: 'queued', error: 'monthly_limit' }])).toBe(true);
  });

  it('ignores other errors and jobs that are not queued', () => {
    expect(hasRefusedJobs([{ status: 'queued', error: 'network' }])).toBe(false);
    expect(hasRefusedJobs([{ status: 'failed', error: 'daily_limit' }])).toBe(false);
    expect(hasRefusedJobs([{ status: 'queued' }])).toBe(false);
  });
});
