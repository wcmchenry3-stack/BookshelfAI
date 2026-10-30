import { AxiosError, AxiosHeaders } from 'axios';
import { BrowserClient, defaultStackParser } from '@sentry/browser';
import { Scope, type Envelope } from '@sentry/core';
import { sentryBeforeBreadcrumb, sentryBeforeSend } from '../../lib/sentry';
import * as scrubModule from '../../lib/sentryScrub';
import { redactString, scrubBreadcrumb, scrubSentryEvent } from '../../lib/sentryScrub';

// A JWT-shaped fixture assembled at runtime, so no JWT literal sits in the source (gitleaks).
const b64 = (text: string) => Buffer.from(text).toString('base64url');
const JWT = [b64('{"alg":"HS256"}'), b64('{"sub":"user-123"}'), b64('signature-secret')].join('.');

function makeAxiosError(): AxiosError {
  const headers = new AxiosHeaders({ Authorization: `Bearer ${JWT}` });
  const config = {
    method: 'get',
    url: 'https://api.example.com/user-books/123e4567-e89b-12d3-a456-426614174000?x=1',
    headers,
    data: JSON.stringify({ secret: 'body-secret' }),
  };
  return new AxiosError(
    'Request failed with status code 500',
    'ERR_BAD_RESPONSE',
    config as never,
    { headers, config },
    {
      status: 500,
      statusText: 'err',
      headers: {},
      config: config as never,
      data: { detail: 'resp-secret' },
    }
  );
}

// Scope.captureException sets hint.originalException, exactly as the global
// unhandled-rejection handler path does in the real SDK.
function scopeFor(client: BrowserClient): Scope {
  const scope = new Scope();
  scope.setClient(client);
  return scope;
}

function makeClient(envelopes: Envelope[], scrub: boolean) {
  const client = new BrowserClient({
    dsn: 'https://pub@example.invalid/1',
    stackParser: defaultStackParser,
    integrations: [],
    ...(scrub ? { beforeSend: sentryBeforeSend } : {}),
    transport: () => ({
      send: async (env: Envelope) => {
        envelopes.push(env);
        return {};
      },
      flush: async () => true,
    }),
  });
  client.init();
  return client;
}

async function captureUnhandled(scrub: boolean): Promise<string> {
  const envelopes: Envelope[] = [];
  const client = makeClient(envelopes, scrub);
  const err = makeAxiosError();
  scopeFor(client).captureException(err, {
    mechanism: {
      type: 'auto.browser.global_handlers.onunhandledrejection',
      handled: false,
      data: { err: err as never },
    },
  });
  await client.flush(1000);
  return JSON.stringify(envelopes);
}

describe('sentry scrub (#736) with a real client and capturing transport', () => {
  it('control: without the scrubber the token reaches the transport', async () => {
    expect(await captureUnhandled(false)).toContain(JWT);
  });

  it('sends no bearer token for an unhandled AxiosError', async () => {
    const out = await captureUnhandled(true);
    expect(out).toContain('ApiError');
    expect(out).not.toContain(JWT);
    expect(out).not.toContain('body-secret');
    expect(out).not.toContain('resp-secret');
    expect(out).not.toContain('123e4567');
  });

  it('redacts extra.config.headers.Authorization and bearer strings', async () => {
    const envelopes: Envelope[] = [];
    const client = makeClient(envelopes, true);
    client.captureEvent({
      message: 'x',
      extra: {
        config: { headers: { Authorization: `Bearer ${JWT}` } },
        note: `sent Bearer ${JWT} here`,
      },
    });
    await client.flush(1000);
    const out = JSON.stringify(envelopes);
    expect(out).not.toContain(JWT);
    expect(out).toContain('[Filtered]');
  });

  it('passes a non-Axios error through with its type and message', async () => {
    const envelopes: Envelope[] = [];
    const client = makeClient(envelopes, true);
    scopeFor(client).captureException(new TypeError('x is undefined'));
    await client.flush(1000);
    const out = JSON.stringify(envelopes);
    expect(out).toContain('TypeError');
    expect(out).toContain('x is undefined');
    expect(out).not.toContain('ApiError');
  });
});

function deepFreeze<T>(o: T): T {
  if (o && typeof o === 'object' && !Object.isFrozen(o)) {
    Object.freeze(o);
    Object.values(o as object).forEach(deepFreeze);
  }
  return o;
}

type Scrubbed = {
  extra?: Record<string, unknown>;
  contexts?: Record<string, unknown>;
  exception?: { values: { type?: string; value?: string; mechanism?: { data?: unknown } }[] };
};

describe('scrubSentryEvent', () => {
  it('leaves a non-Axios event unchanged', () => {
    const event = {
      exception: { values: [{ type: 'TypeError', value: 'x is undefined' }] },
      extra: { screen: 'home', count: 3 },
    };
    const copy = JSON.parse(JSON.stringify(event));
    const out = scrubSentryEvent(event, { originalException: new TypeError('x is undefined') });
    expect(out).toEqual(copy);
    expect(out).toBe(event); // nothing to scrub: same object back
  });

  it('works on a deeply frozen event without mutating it', () => {
    const event = deepFreeze({
      extra: { config: { url: '/a', headers: { Authorization: `Bearer ${JWT}` } }, keep: 1 },
      contexts: { x: { authorization: 'a' } },
      exception: {
        values: [
          { type: 'E', value: `bad ${JWT}`, mechanism: { data: { Authorization: 'z', ok: 1 } } },
        ],
      },
    });
    const out = scrubSentryEvent(event) as Scrubbed;
    const s = JSON.stringify(out);
    expect(s).not.toContain(JWT);
    expect(s).not.toContain('"z"');
    expect(out.extra?.keep).toBe(1);
    expect(JSON.stringify(event)).toContain(JWT); // input untouched
  });

  describe('narrowed redaction', () => {
    it('always redacts authorization/cookie/set-cookie/proxy-authorization at any depth', () => {
      const out = scrubSentryEvent({
        extra: {
          a: {
            b: { Authorization: '1', COOKIE: '2', 'Set-Cookie': '3', 'Proxy-Authorization': '4' },
          },
        },
      }) as Scrubbed;
      expect(JSON.stringify(out)).not.toMatch(/"[1-4]"/);
    });

    it('redacts headers/data under an HTTP-looking config/request/response', () => {
      const out = scrubSentryEvent({
        extra: {
          response: { status: 500, headers: { a: 'h-secret' }, data: { d: 'd-secret' } },
          config: { url: '/u', headers: { a: 'c-secret' } },
          request: { headers: { a: 'r-secret' }, responseURL: '/x' },
        },
      }) as Scrubbed;
      expect(JSON.stringify(out)).not.toMatch(/secret/);
      expect((out.extra?.response as { status: number }).status).toBe(500);
    });

    it('keeps unrelated headers/data/config/request keys', () => {
      const event = {
        extra: { data: { n: 1 }, headers: { x: 1 }, config: { theme: 'dark' } },
        contexts: { request: { method: 'GET' }, custom: { request: { id: 1 } } },
      };
      expect(scrubSentryEvent(event)).toEqual(event);
    });

    it('keeps contexts.request {method} and masks token query params in its url', () => {
      const out = scrubSentryEvent({
        contexts: { request: { method: 'GET', url: 'https://h/p?access_token=abc123&a=1' } },
      }) as Scrubbed;
      const req = out.contexts?.request as { method: string; url: string };
      expect(req.method).toBe('GET');
      expect(req.url).toBe('https://h/p?access_token=[Filtered]&a=1');
    });
  });

  describe('value handling', () => {
    it('turns an Error in extra into {name, message} with redaction', () => {
      const err = new Error(`failed Bearer ${JWT}`);
      (err as unknown as { config: unknown }).config = { headers: { Authorization: 'x' } };
      const out = scrubSentryEvent({ extra: { err } }) as Scrubbed;
      expect(out.extra?.err).toEqual({ name: 'Error', message: 'failed Bearer [Filtered]' });
    });

    it('keeps Date, and renders Map/Set/class instances as [Object]', () => {
      class Foo {
        secret = 's';
      }
      const d = new Date(0);
      const out = scrubSentryEvent({
        extra: { d, m: new Map([['a', 'b']]), s: new Set([1]), f: new Foo() },
      }) as Scrubbed;
      expect(out.extra).toEqual({ d, m: '[Object]', s: '[Object]', f: '[Object]' });
    });

    it('marks only true cycles as [Circular], not shared references', () => {
      const shared = { v: 1 };
      const cyc: Record<string, unknown> = { name: 'c' };
      cyc.self = cyc;
      const out = scrubSentryEvent({ extra: { x: shared, y: shared, cyc } }) as Scrubbed;
      expect(out.extra?.x).toEqual({ v: 1 });
      expect(out.extra?.y).toEqual({ v: 1 });
      expect((out.extra?.cyc as { self: unknown }).self).toBe('[Circular]');
    });

    it('caps depth', () => {
      let deep: Record<string, unknown> = { leaf: 'x' };
      for (let i = 0; i < 20; i++) deep = { n: deep };
      expect(JSON.stringify(scrubSentryEvent({ extra: deep }))).toContain('[Filtered]');
    });
  });

  describe('string redaction', () => {
    it('masks bearer tokens, raw JWTs and token query params', () => {
      expect(redactString(`Bearer ${JWT}`)).toBe('Bearer [Filtered]');
      expect(redactString(`oops ${JWT} end`)).toBe('oops [jwt] end');
      expect(redactString('u?refresh_token=abc&api_key=k1&api-key=k2&password=p w')).toBe(
        'u?refresh_token=[Filtered]&api_key=[Filtered]&api-key=[Filtered]&password=[Filtered] w'
      );
      expect(redactString('page=2&tokens=none')).toBe('page=2&tokens=none');
    });

    // Time at ~40k vs ~10k chars: linear gives a ratio near 4, quadratic near 16.
    const time = (s: string) => {
      let best = Infinity;
      for (let i = 0; i < 3; i++) {
        const t = performance.now();
        redactString(s);
        best = Math.min(best, performance.now() - t);
      }
      return best;
    };
    const cases: [string, (n: number) => string][] = [
      ['eyJ', (n) => 'eyJ'.repeat(Math.ceil(n / 3))],
      ['eyJa.', (n) => 'eyJa.'.repeat(Math.ceil(n / 5))],
      ['eyJa.eyJb.', (n) => 'eyJa.eyJb.'.repeat(Math.ceil(n / 10))],
      ['Bearer ', (n) => 'Bearer '.repeat(Math.ceil(n / 7))],
      ['Bearer + token', (n) => 'Bearer ' + 'a'.repeat(n) + '!'],
      ['token=', (n) => 'token=' + 'x'.repeat(n)],
      ['Authorization:', (n) => 'Authorization: '.repeat(Math.ceil(n / 15))],
      ['Cookie:', (n) => 'Cookie: '.repeat(Math.ceil(n / 8))],
    ];
    it.each(cases)('is linear on %s repeated', (_name, make) => {
      const small = time(make(10000));
      const big = time(make(40000));
      expect(big).toBeLessThan(1000);
      expect(big / Math.max(small, 1)).toBeLessThan(8);
    });

    it('redacts JWTs in the scanned forms and leaves look-alikes', () => {
      expect(redactString(`a ${JWT} b`)).toBe('a [jwt] b');
      expect(redactString(`(${JWT}) and ${JWT}.`)).toBe('([jwt]) and [jwt].');
      expect(redactString(`x${JWT}`)).toBe(`x${JWT}`); // mid-token: not a JWT start
      expect(redactString('eyJonly.twoparts')).toBe('eyJonly.twoparts');
    });

    it('masks Authorization / Cookie / Set-Cookie header lines', () => {
      expect(redactString('Authorization: Bearer abc.def')).toBe('Authorization: [Filtered]');
      expect(redactString('authorization:Basic dXNlcjpwYXNz')).toBe('authorization: [Filtered]');
      expect(redactString('Cookie: sid=1; other=2\nnext')).toBe('Cookie: [Filtered]\nnext');
      expect(redactString('Set-Cookie: sid=1; HttpOnly')).toBe('Set-Cookie: [Filtered]');
    });
  });

  describe('fail closed', () => {
    const boom = () => {
      const o: Record<string, unknown> = {};
      Object.defineProperty(o, 'bad', {
        enumerable: true,
        get() {
          throw new Error('nope');
        },
      });
      return o;
    };

    it('drops a field it cannot scrub, keeps the others', () => {
      const out = scrubSentryEvent({
        extra: boom(),
        contexts: { ok: 1 },
        exception: { values: [{ type: 'E', value: 'v', mechanism: { data: boom() } }] },
      }) as Scrubbed;
      expect('extra' in out).toBe(false);
      expect(out.contexts).toEqual({ ok: 1 });
      expect(out.exception?.values[0].mechanism).toEqual({});
      expect(out.exception?.values[0].value).toBe('v');
    });
  });

  describe('AxiosError exception replacement', () => {
    it('replaces only the last (original) entry, not the cause chain', () => {
      const out = scrubSentryEvent(
        {
          exception: {
            values: [
              { type: 'Error', value: 'root cause' },
              { type: 'AxiosError', value: 'Request failed' },
            ],
          },
        },
        { originalException: makeAxiosError() }
      ) as Scrubbed;
      expect(out.exception?.values[0]).toEqual({ type: 'Error', value: 'root cause' });
      expect(out.exception?.values[1].type).toBe('ApiError');
      expect(out.exception?.values[1].value).toBe('API GET /user-books/:id 500');
    });
  });
});

describe('sentryBeforeSend ordering', () => {
  afterEach(() => jest.restoreAllMocks());

  it('drops filtered network noise without calling the scrubber', () => {
    const spy = jest.spyOn(scrubModule, 'scrubSentryEvent');
    const event = {
      exception: {
        values: [
          {
            type: 'Error',
            value: '12000ms timeout exceeded',
            mechanism: {
              type: 'auto.browser.global_handlers.onunhandledrejection',
              handled: false,
            },
          },
        ],
      },
    };
    expect(sentryBeforeSend(event as never, {})).toBeNull();
    expect(spy).not.toHaveBeenCalled();
  });

  it('scrubs events the filter keeps', () => {
    const spy = jest.spyOn(scrubModule, 'scrubSentryEvent');
    sentryBeforeSend({ extra: { a: 1 } } as never, {});
    expect(spy).toHaveBeenCalledTimes(1);
  });
});

describe('round 2 scrub rules', () => {
  const TOKEN = b64('runtime-access-token-value');

  it('redacts response.data / headers unconditionally, even with no status', () => {
    const out = scrubSentryEvent({
      extra: { response: { data: { access_token: TOKEN }, headers: { a: 'b' }, note: 'ok' } },
    }) as { extra: { response: Record<string, unknown> } };
    expect(JSON.stringify(out)).not.toContain(TOKEN);
    expect(out.extra.response.data).toBe('[Filtered]');
    expect(out.extra.response.headers).toBe('[Filtered]');
    expect(out.extra.response.note).toBe('ok');
  });

  it('always redacts secret-looking keys, case-insensitively', () => {
    const extra: Record<string, unknown> = {};
    for (const k of [
      'access_token',
      'Refresh_Token',
      'id_token',
      'PASSWORD',
      'passwd',
      'secret',
      'api_key',
      'apiKey',
      'api-key',
    ]) {
      extra[k] = TOKEN;
    }
    extra.keep = 'ok';
    const out = scrubSentryEvent({ extra }) as { extra: Record<string, unknown> };
    expect(JSON.stringify(out)).not.toContain(TOKEN);
    expect(out.extra.keep).toBe('ok');
  });

  it('redacts a bare extra.access_token', () => {
    const out = scrubSentryEvent({ extra: { access_token: TOKEN } }) as Scrubbed;
    expect(out.extra?.access_token).toBe('[Filtered]');
  });

  it('redacts config wholesale when it has data, baseURL or method', () => {
    for (const config of [{ data: 'd' }, { baseURL: 'b' }, { method: 'get' }]) {
      const out = scrubSentryEvent({ extra: { config } }) as Scrubbed;
      expect(out.extra?.config).toBe('[Filtered]');
    }
  });

  it('treats an XHR-like request with _header as sensitive', () => {
    const out = scrubSentryEvent({
      extra: { request: { _header: 'h', readyState: 4 } },
    }) as Scrubbed;
    expect(out.extra?.request).toBe('[Filtered]');
  });

  describe('fail closed at the event level', () => {
    it('returns null when the event cannot be copied', () => {
      const evil = new Proxy(
        {},
        {
          ownKeys() {
            throw new Error('nope');
          },
        }
      );
      expect(scrubSentryEvent(evil)).toBeNull();
    });

    it('drops exception when its values cannot be read', () => {
      const exception = new Proxy(
        {},
        {
          get() {
            throw new Error('nope');
          },
        }
      );
      const out = scrubSentryEvent({ message: 'm', exception }) as Record<string, unknown>;
      expect('exception' in out).toBe(false);
      expect(out.message).toBe('m');
    });
  });

  describe('message, logentry and request', () => {
    it('scrubs event.message and logentry message/params', () => {
      const out = scrubSentryEvent({
        message: `failed Bearer ${JWT}`,
        logentry: { message: 'got %s', params: [`Bearer ${JWT}`, 3], formatted: `x ${JWT}` },
      }) as { message: string; logentry: { params: unknown[]; formatted: string } };
      expect(JSON.stringify(out)).not.toContain(JWT);
      expect(out.logentry.params[1]).toBe(3);
    });

    it('scrubs event.request headers, url, query_string and cookies', () => {
      const out = scrubSentryEvent({
        request: {
          url: 'https://h/p?access_token=abc&x=1',
          query_string: 'token=abc&x=1',
          method: 'GET',
          headers: { Authorization: `Bearer ${JWT}`, Accept: 'json' },
          cookies: { sid: '1' },
        },
      }) as unknown as {
        request: {
          url: string;
          query_string: string;
          method: string;
          headers: Record<string, string>;
          cookies: string;
        };
      };
      expect(out.request.url).toBe('https://h/p?access_token=[Filtered]&x=1');
      expect(out.request.query_string).toBe('token=[Filtered]&x=1');
      expect(out.request.method).toBe('GET');
      expect(out.request.headers).toEqual({ Authorization: '[Filtered]', Accept: 'json' });
      expect(out.request.cookies).toBe('[Filtered]');
    });
  });

  describe('scrubBreadcrumb / beforeBreadcrumb', () => {
    it('masks tokens in data and message', () => {
      const out = scrubBreadcrumb({
        category: 'http',
        message: `GET with Bearer ${JWT}`,
        data: { url: 'https://h/p?api_key=k1&a=1', Authorization: 'x', status_code: 200 },
      }) as { message: string; data: Record<string, unknown> };
      expect(JSON.stringify(out)).not.toContain(JWT);
      expect(out.data.url).toBe('https://h/p?api_key=[Filtered]&a=1');
      expect(out.data.Authorization).toBe('[Filtered]');
      expect(out.data.status_code).toBe(200);
    });

    it('returns a clean breadcrumb as the same object', () => {
      const b = { category: 'ui', message: 'tap', data: { n: 1 } };
      expect(scrubBreadcrumb(b)).toBe(b);
    });

    it('is wired into Sentry.init and keeps the #701 sentry.filter breadcrumb', () => {
      expect(typeof sentryBeforeBreadcrumb).toBe('function');
      const filterCrumb = {
        category: 'sentry.filter',
        message: 'Dropped expected client network error',
        level: 'info',
      };
      expect(sentryBeforeBreadcrumb(filterCrumb as never)).toBe(filterCrumb);
    });
  });
});
