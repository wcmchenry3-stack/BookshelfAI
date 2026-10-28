import React from 'react';
import { act, renderHook, screen } from '@testing-library/react-native';

import { resetTurnstileLoggingForTests, TurnstileProvider } from '../../contexts/TurnstileContext';
import { useTurnstile } from '../../hooks/useTurnstile';
import {
  originOf,
  TURNSTILE_TOKEN_TIMEOUT_MS,
  turnstileBaseUrl,
  turnstileSiteKey,
} from '../../lib/turnstile/config';
import {
  isAllowedTurnstileNavigation,
  turnstileOriginWhitelist,
} from '../../lib/turnstile/navigationGuard';
import { TurnstileTokenBroker, type TurnstileSolver } from '../../lib/turnstile/tokenBroker';
import { buildTurnstileHtml, parseTurnstileMessage } from '../../lib/turnstile/widgetHtml';

// #740: the Turnstile token provider (broker, config, hidden WebView widget).

const mockBreadcrumb = jest.fn();
jest.mock('../../lib/sentry', () => ({
  Sentry: { addBreadcrumb: (...a: unknown[]) => mockBreadcrumb(...a) },
}));

const { __mock: webView } = jest.requireMock('react-native-webview') as {
  __mock: { injectJavaScript: jest.Mock; reload: jest.Mock };
};

const BASE = 'https://bookshelfapi.example.com';
const SITE_KEY = '1x00000000000000000000AA';

const ENV_KEYS = [
  'EXPO_PUBLIC_TURNSTILE_SITE_KEY',
  'EXPO_PUBLIC_TURNSTILE_BASE_URL',
  'EXPO_PUBLIC_API_URL',
] as const;
const savedEnv: Partial<Record<(typeof ENV_KEYS)[number], string | undefined>> = {};

beforeEach(() => {
  jest.resetAllMocks();
  resetTurnstileLoggingForTests();
  for (const k of ENV_KEYS) {
    savedEnv[k] = process.env[k];
    delete process.env[k];
  }
});

afterEach(() => {
  jest.useRealTimers();
  for (const k of ENV_KEYS) {
    if (savedEnv[k] === undefined) delete process.env[k];
    else process.env[k] = savedEnv[k];
  }
});

function fakeSolver() {
  const solver = { start: jest.fn(), reset: jest.fn() } satisfies TurnstileSolver;
  return solver;
}

describe('TurnstileTokenBroker', () => {
  it('runs one challenge per request and resolves with its token', async () => {
    const broker = new TurnstileTokenBroker();
    const solver = fakeSolver();
    broker.attach(solver);
    broker.setReady(true);
    const first = broker.request();
    expect(solver.start).toHaveBeenCalledWith(1);
    broker.deliver(1, 'tok-a');
    await expect(first).resolves.toBe('tok-a');

    const second = broker.request();
    expect(solver.start).toHaveBeenLastCalledWith(2);
    broker.deliver(2, 'tok-b');
    await expect(second).resolves.toBe('tok-b');
  });

  it('runs challenges one at a time, in order', async () => {
    const broker = new TurnstileTokenBroker();
    const solver = fakeSolver();
    broker.attach(solver);
    broker.setReady(true);
    const a = broker.request();
    const b = broker.request();
    expect(solver.start).toHaveBeenCalledTimes(1);
    broker.deliver(1, 'tok-a');
    await expect(a).resolves.toBe('tok-a');
    expect(solver.start).toHaveBeenLastCalledWith(2);
    broker.deliver(2, 'tok-b');
    await expect(b).resolves.toBe('tok-b');
  });

  it('waits for the widget to be ready', async () => {
    const broker = new TurnstileTokenBroker();
    const solver = fakeSolver();
    broker.attach(solver);
    const p = broker.request();
    expect(solver.start).not.toHaveBeenCalled();
    broker.setReady(true);
    expect(solver.start).toHaveBeenCalledWith(1);
    broker.deliver(1, 'tok');
    await expect(p).resolves.toBe('tok');
  });

  it('resolves null after the timeout, resets the widget, and drops a late token', async () => {
    jest.useFakeTimers();
    const broker = new TurnstileTokenBroker();
    const solver = fakeSolver();
    broker.attach(solver);
    broker.setReady(true);
    const p = broker.request();
    jest.advanceTimersByTime(TURNSTILE_TOKEN_TIMEOUT_MS);
    await expect(p).resolves.toBeNull();
    expect(solver.reset).toHaveBeenCalledTimes(1);
    expect(mockBreadcrumb).toHaveBeenCalledWith(
      expect.objectContaining({
        category: 'turnstile',
        message: expect.stringContaining('timeout'),
      })
    );

    const next = broker.request();
    broker.deliver(1, 'late'); // the timed-out request's token
    broker.deliver(2, 'fresh');
    await expect(next).resolves.toBe('fresh');
  });

  it('the timeout is about 10 s', () => {
    expect(TURNSTILE_TOKEN_TIMEOUT_MS).toBe(10_000);
  });

  it('resolves null on abort and never hands that token to a later request', async () => {
    const broker = new TurnstileTokenBroker();
    const solver = fakeSolver();
    broker.attach(solver);
    broker.setReady(true);
    const controller = new AbortController();
    const p = broker.request(controller.signal);
    controller.abort();
    await expect(p).resolves.toBeNull();
    expect(solver.reset).toHaveBeenCalled();

    const next = broker.request();
    broker.deliver(1, 'tok-for-aborted');
    broker.deliver(2, 'tok-next');
    await expect(next).resolves.toBe('tok-next');
  });

  it('an already-aborted signal resolves null without a challenge', async () => {
    const broker = new TurnstileTokenBroker();
    const solver = fakeSolver();
    broker.attach(solver);
    broker.setReady(true);
    const controller = new AbortController();
    controller.abort();
    await expect(broker.request(controller.signal)).resolves.toBeNull();
    expect(solver.start).not.toHaveBeenCalled();
  });

  it('a widget error, or an empty token, resolves null', async () => {
    const broker = new TurnstileTokenBroker();
    broker.attach(fakeSolver());
    broker.setReady(true);
    const a = broker.request();
    broker.fail(1);
    await expect(a).resolves.toBeNull();
    const b = broker.request();
    broker.deliver(2, '');
    await expect(b).resolves.toBeNull();
    const c = broker.request();
    broker.failCurrent();
    await expect(c).resolves.toBeNull();
  });

  it('detaching the widget resolves every waiting request with null', async () => {
    const broker = new TurnstileTokenBroker();
    const detach = broker.attach(fakeSolver());
    broker.setReady(true);
    const a = broker.request();
    const b = broker.request();
    detach();
    await expect(a).resolves.toBeNull();
    await expect(b).resolves.toBeNull();
  });

  it('a page reload mid-challenge re-runs it once the page is ready again', async () => {
    const broker = new TurnstileTokenBroker();
    const solver = fakeSolver();
    broker.attach(solver);
    broker.setReady(true);
    const p = broker.request();
    broker.setReady(false);
    broker.setReady(true);
    expect(solver.start).toHaveBeenCalledTimes(2);
    broker.deliver(1, 'tok');
    await expect(p).resolves.toBe('tok');
  });
});

describe('Turnstile config', () => {
  it('reads the site key, and ignores an implausible one', () => {
    expect(turnstileSiteKey()).toBeNull();
    process.env.EXPO_PUBLIC_TURNSTILE_SITE_KEY = SITE_KEY;
    expect(turnstileSiteKey()).toBe(SITE_KEY);
    process.env.EXPO_PUBLIC_TURNSTILE_SITE_KEY = '"</script><script>alert(1)';
    expect(turnstileSiteKey()).toBeNull();
    process.env.EXPO_PUBLIC_TURNSTILE_SITE_KEY = '   ';
    expect(turnstileSiteKey()).toBeNull();
  });

  it('the base URL defaults to the API origin', () => {
    expect(turnstileBaseUrl()).toBeNull();
    process.env.EXPO_PUBLIC_API_URL = 'https://bookshelfapi.buffingchi.com/';
    expect(turnstileBaseUrl()).toBe('https://bookshelfapi.buffingchi.com');
    process.env.EXPO_PUBLIC_TURNSTILE_BASE_URL = 'https://App.Example.com/some/path';
    expect(turnstileBaseUrl()).toBe('https://app.example.com');
  });

  it('the base URL must be https (http only for localhost)', () => {
    process.env.EXPO_PUBLIC_TURNSTILE_BASE_URL = 'http://bookshelf.example.com';
    expect(turnstileBaseUrl()).toBeNull();
    process.env.EXPO_PUBLIC_TURNSTILE_BASE_URL = 'http://localhost:8000';
    expect(turnstileBaseUrl()).toBe('http://localhost:8000');
  });

  it('originOf rejects non-http URLs and credentials', () => {
    expect(originOf('https://a.example.com:8443/x?y#z')).toBe('https://a.example.com:8443');
    expect(originOf('javascript:alert(1)')).toBeNull();
    expect(originOf('https://user@evil.example.com')).toBeNull();
    expect(originOf('file:///etc/passwd')).toBeNull();
    expect(originOf('')).toBeNull();
  });
});

describe('WebView navigation guard', () => {
  it.each([
    [BASE, true],
    [`${BASE}/`, true],
    ['about:blank', true],
    ['about:srcdoc', true],
    ['https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/if/ov2', true],
    ['https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit', true],
    ['https://evil.example.com/', false],
    ['https://challenges.cloudflare.com.evil.example.com/', false],
    ['https://bookshelfapi.example.com.evil.example.com/', false],
    ['http://challenges.cloudflare.com/', false],
    ['http://bookshelfapi.example.com/', false],
    ['https://www.cloudflare.com/privacypolicy/', false],
    ['file:///data/user/0/app/files/secret', false],
    ['javascript:alert(1)', false],
    ['data:text/html,<script>1</script>', false],
    ['intent://scan/#Intent;scheme=zxing;end', false],
    ['tel:123', false],
  ])('%s -> %s', (url, allowed) => {
    expect(isAllowedTurnstileNavigation(url, BASE)).toBe(allowed);
  });

  it('the origin whitelist lists only the page and Turnstile', () => {
    expect(turnstileOriginWhitelist(BASE)).toEqual([
      BASE,
      'https://challenges.cloudflare.com',
      'about:srcdoc',
    ]);
  });
});

describe('widget page', () => {
  it('renders one invisible, execute-mode widget with the site key', () => {
    const html = buildTurnstileHtml(SITE_KEY);
    expect(html).toContain(`var SITE_KEY = "${SITE_KEY}"`);
    expect(html).toContain("execution: 'execute'");
    expect(html).toContain(
      'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit&onload='
    );
    // Reset before every execute, so each attempt gets a fresh token.
    expect(html).toMatch(/turnstile\.reset\(widgetId\);\s*window\.turnstile\.execute\(widgetId\)/);
  });

  it('parses only its own messages', () => {
    expect(parseTurnstileMessage('{"type":"ready"}')).toEqual({ type: 'ready' });
    expect(parseTurnstileMessage('{"type":"token","id":3,"token":"t"}')).toEqual({
      type: 'token',
      id: 3,
      token: 't',
    });
    expect(parseTurnstileMessage('{"type":"token","id":"3","token":"t"}')).toBeNull();
    expect(parseTurnstileMessage('not json')).toBeNull();
    expect(parseTurnstileMessage({ type: 'ready' })).toBeNull();
  });
});

describe('TurnstileProvider (native)', () => {
  function wrapper({ children }: { children: React.ReactNode }) {
    return <TurnstileProvider>{children}</TurnstileProvider>;
  }
  const webViewEl = () => screen.getByTestId('turnstile-webview', { includeHiddenElements: true });
  function fromPage(msg: object, url = BASE) {
    webViewEl().props.onMessage({ nativeEvent: { data: JSON.stringify(msg), url } });
  }

  it('returns the token the WebView posts back', async () => {
    process.env.EXPO_PUBLIC_TURNSTILE_SITE_KEY = SITE_KEY;
    process.env.EXPO_PUBLIC_TURNSTILE_BASE_URL = BASE;
    const { result } = await renderHook(() => useTurnstile(), { wrapper });
    expect(webViewEl().props.source).toEqual({ html: expect.any(String), baseUrl: BASE });
    fromPage({ type: 'ready' });
    let token: Promise<string | null> = Promise.resolve(null);
    await act(async () => {
      token = result.current.getToken();
    });
    expect(webView.injectJavaScript).toHaveBeenCalledWith(expect.stringContaining('.run(1)'));
    fromPage({ type: 'token', id: 1, token: 'tok-1' });
    await expect(token).resolves.toBe('tok-1');
  });

  it('ignores messages from any other origin (e.g. a frame)', async () => {
    jest.useFakeTimers();
    process.env.EXPO_PUBLIC_TURNSTILE_SITE_KEY = SITE_KEY;
    process.env.EXPO_PUBLIC_TURNSTILE_BASE_URL = BASE;
    const { result } = await renderHook(() => useTurnstile(), { wrapper });
    fromPage({ type: 'ready' });
    let token: Promise<string | null> = Promise.resolve(null);
    await act(async () => {
      token = result.current.getToken();
    });
    fromPage({ type: 'token', id: 1, token: 'forged' }, 'https://evil.example.com/');
    await act(async () => {
      await jest.advanceTimersByTimeAsync(TURNSTILE_TOKEN_TIMEOUT_MS);
    });
    await expect(token).resolves.toBeNull();
  });

  it('is locked down: guarded navigation, limited origins, no file access, no windows', async () => {
    process.env.EXPO_PUBLIC_TURNSTILE_SITE_KEY = SITE_KEY;
    process.env.EXPO_PUBLIC_TURNSTILE_BASE_URL = BASE;
    await renderHook(() => useTurnstile(), { wrapper });
    const props = webViewEl().props;
    expect(props.originWhitelist).toEqual([
      BASE,
      'https://challenges.cloudflare.com',
      'about:srcdoc',
    ]);
    expect(props.onShouldStartLoadWithRequest({ url: 'https://evil.example.com/' })).toBe(false);
    expect(props.onShouldStartLoadWithRequest({ url: 'file:///etc/hosts' })).toBe(false);
    expect(
      props.onShouldStartLoadWithRequest({ url: 'https://challenges.cloudflare.com/cdn-cgi/x' })
    ).toBe(true);
    expect(props.onShouldStartLoadWithRequest({ url: BASE })).toBe(true);
    expect(props).toEqual(
      expect.objectContaining({
        javaScriptEnabled: true,
        javaScriptCanOpenWindowsAutomatically: false,
        setSupportMultipleWindows: false,
        allowFileAccess: false,
        allowFileAccessFromFileURLs: false,
        allowUniversalAccessFromFileURLs: false,
        mixedContentMode: 'never',
      })
    );
  });

  it('one WebView however many tokens are requested', async () => {
    jest.useFakeTimers();
    process.env.EXPO_PUBLIC_TURNSTILE_SITE_KEY = SITE_KEY;
    process.env.EXPO_PUBLIC_TURNSTILE_BASE_URL = BASE;
    const { result } = await renderHook(() => useTurnstile(), { wrapper });
    fromPage({ type: 'ready' });
    await act(async () => {
      void result.current.getToken();
      void result.current.getToken();
    });
    expect(
      screen.getAllByTestId('turnstile-webview', { includeHiddenElements: true })
    ).toHaveLength(1);
  });

  it('without a site key: no WebView, null at once, one breadcrumb', async () => {
    process.env.EXPO_PUBLIC_API_URL = BASE;
    const { result } = await renderHook(() => useTurnstile(), { wrapper });
    expect(screen.queryByTestId('turnstile-webview', { includeHiddenElements: true })).toBeNull();
    await expect(result.current.getToken()).resolves.toBeNull();
    await expect(result.current.getToken()).resolves.toBeNull();
    expect(mockBreadcrumb).toHaveBeenCalledTimes(1);
    expect(mockBreadcrumb).toHaveBeenCalledWith(
      expect.objectContaining({ message: expect.stringContaining('site_key') })
    );
  });

  it('with a site key but no usable base URL: no WebView, null at once', async () => {
    process.env.EXPO_PUBLIC_TURNSTILE_SITE_KEY = SITE_KEY;
    process.env.EXPO_PUBLIC_API_URL = 'http://bookshelf.example.com';
    const { result } = await renderHook(() => useTurnstile(), { wrapper });
    expect(screen.queryByTestId('turnstile-webview', { includeHiddenElements: true })).toBeNull();
    await expect(result.current.getToken()).resolves.toBeNull();
  });

  it('without a provider, getToken is null (no widget anywhere)', async () => {
    const { result } = await renderHook(() => useTurnstile());
    await expect(result.current.getToken()).resolves.toBeNull();
  });
});
