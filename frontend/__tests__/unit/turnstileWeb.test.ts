/**
 * @jest-environment jsdom
 */
import { TurnstileTokenBroker } from '../../lib/turnstile/tokenBroker';
import {
  loadTurnstileScript,
  resetTurnstileScriptForTests,
  type TurnstileApi,
} from '../../lib/turnstile/webScript';
import { mountWebTurnstile } from '../../lib/turnstile/webWidget';

// #740, web: Cloudflare's script is loaded once and one invisible widget is
// reset and executed per /scan attempt.

jest.mock('../../lib/sentry', () => ({ Sentry: { addBreadcrumb: jest.fn() } }));

/** A stand-in for window.turnstile: execute() answers with `web-<n>`. */
function fakeTurnstile() {
  let options: Record<string, unknown> = {};
  let n = 0;
  const api = {
    render: jest.fn((_c: HTMLElement | string, o: Record<string, unknown>) => {
      options = o;
      return 'widget-1';
    }),
    reset: jest.fn(),
    remove: jest.fn(),
    execute: jest.fn(() => {
      n += 1;
      const token = `web-${n}`;
      Promise.resolve().then(() => (options.callback as (t: string) => void)(token));
    }),
    fireError: () => (options['error-callback'] as () => void)(),
    options: () => options,
  };
  return api;
}

const flush = () => new Promise((r) => setTimeout(r, 0));

beforeEach(() => {
  resetTurnstileScriptForTests();
  delete (window as { turnstile?: TurnstileApi }).turnstile;
  document.head.innerHTML = '';
  document.body.innerHTML = '';
});

describe('mountWebTurnstile', () => {
  it('uses the loaded widget: a fresh token per request', async () => {
    const ts = fakeTurnstile();
    const load = jest.fn(() => Promise.resolve(ts as unknown as TurnstileApi));
    const broker = new TurnstileTokenBroker();
    const cleanup = mountWebTurnstile(broker, '1x00000000000000000000AA', load);
    await flush();

    expect(load).toHaveBeenCalledTimes(1);
    expect(ts.render).toHaveBeenCalledWith(
      expect.any(HTMLElement),
      expect.objectContaining({ sitekey: '1x00000000000000000000AA', execution: 'execute' })
    );
    await expect(broker.request()).resolves.toBe('web-1');
    await expect(broker.request()).resolves.toBe('web-2');
    // Reset before each execute.
    expect(ts.reset).toHaveBeenCalledTimes(2);
    expect(ts.execute).toHaveBeenCalledTimes(2);

    cleanup();
    expect(ts.remove).toHaveBeenCalledWith('widget-1');
    expect(document.body.children).toHaveLength(0);
  });

  it('a widget error answers null', async () => {
    const ts = fakeTurnstile();
    ts.execute.mockImplementation(() => Promise.resolve().then(ts.fireError));
    const broker = new TurnstileTokenBroker();
    mountWebTurnstile(broker, 'key', () => Promise.resolve(ts as unknown as TurnstileApi));
    await flush();
    await expect(broker.request()).resolves.toBeNull();
  });

  it('a script that fails to load answers null at once, not after the timeout', async () => {
    jest.useFakeTimers();
    try {
      const broker = new TurnstileTokenBroker();
      mountWebTurnstile(broker, 'key', () => Promise.reject(new Error('blocked')));
      await jest.advanceTimersByTimeAsync(0);
      const p = broker.request();
      await jest.advanceTimersByTimeAsync(0);
      await expect(p).resolves.toBeNull();
    } finally {
      jest.useRealTimers();
    }
  });
});

describe('loadTurnstileScript', () => {
  it('adds Cloudflare’s script once and resolves with window.turnstile', async () => {
    const a = loadTurnstileScript();
    const b = loadTurnstileScript();
    expect(a).toBe(b);
    const scripts = document.head.querySelectorAll('script');
    expect(scripts).toHaveLength(1);
    expect(scripts[0].src).toBe(
      'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit'
    );
    const ts = fakeTurnstile();
    (window as { turnstile?: unknown }).turnstile = ts;
    scripts[0].onload?.(new Event('load'));
    await expect(a).resolves.toBe(ts);
    // Loaded: no second script.
    await expect(loadTurnstileScript()).resolves.toBe(ts);
    expect(document.head.querySelectorAll('script')).toHaveLength(1);
  });

  it('tries again after a failed load', async () => {
    const a = loadTurnstileScript();
    document.head.querySelector('script')!.onerror?.(new Event('error'));
    await expect(a).rejects.toThrow('turnstile_script_failed');
    loadTurnstileScript().catch(() => {});
    expect(document.head.querySelectorAll('script')).toHaveLength(2);
  });
});
