import { AxiosError, AxiosHeaders } from 'axios';
import { BrowserClient, defaultStackParser } from '@sentry/browser';
import { Scope, type Envelope } from '@sentry/core';
import { sentryBeforeSend } from '../../lib/sentry';
import { scrubSentryEvent } from '../../lib/sentryScrub';

const JWT = 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEyMyJ9.c2lnbmF0dXJlLXNlY3JldA';

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

describe('scrubSentryEvent', () => {
  it('leaves a non-Axios event unchanged', () => {
    const event = {
      exception: { values: [{ type: 'TypeError', value: 'x is undefined' }] },
      extra: { screen: 'home', count: 3 },
    };
    const copy = JSON.parse(JSON.stringify(event));
    const out = scrubSentryEvent(event, { originalException: new TypeError('x is undefined') });
    expect(out).toEqual(copy);
  });

  it('redacts config/headers/request/response.data and Authorization case variants', () => {
    const out = scrubSentryEvent({
      extra: {
        authorization: 'a',
        Authorization: 'b',
        config: { headers: { Authorization: 'c' } },
        response: { status: 500, data: { detail: 'secret' } },
        keep: 'ok',
      },
      contexts: { custom: { request: { url: 'u' }, headers: { x: 1 } } },
    }) as { extra: Record<string, unknown> };
    const s = JSON.stringify(out);
    expect(s).not.toContain('secret');
    expect(out.extra.keep).toBe('ok');
    expect((out.extra.response as { status: number }).status).toBe(500);
    expect(out.extra.config).toBe('[Filtered]');
    expect(s).not.toContain('"url":"u"');
  });

  it('never throws on cyclic data', () => {
    const a: Record<string, unknown> = {};
    a.self = a;
    expect(() => scrubSentryEvent({ extra: a })).not.toThrow();
  });
});
