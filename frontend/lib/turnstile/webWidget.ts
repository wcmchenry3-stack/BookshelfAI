import { Sentry } from '../sentry';
import type { TurnstileTokenBroker } from './tokenBroker';
import { loadTurnstileScript, type TurnstileApi } from './webScript';

/**
 * Web (#740): load Cloudflare's script once, render one invisible widget
 * (`execution: 'execute'`) in a zero-size container, and attach it to the
 * broker, which resets and executes it once per /scan attempt. Returns the
 * cleanup. If the script cannot load, every request is answered with no
 * token at once (the scan is sent without one) instead of waiting out the
 * timeout.
 */
export function mountWebTurnstile(
  broker: TurnstileTokenBroker,
  siteKey: string,
  load: () => Promise<TurnstileApi> = loadTurnstileScript
): () => void {
  let cancelled = false;
  let detach: (() => void) | null = null;
  let container: HTMLDivElement | null = null;
  let widgetId: string | undefined;
  let api: TurnstileApi | null = null;
  // The broker request the running challenge belongs to.
  let current: number | null = null;

  const settle = (fn: (id: number) => void) => {
    if (current === null) return;
    const id = current;
    current = null;
    fn(id);
  };

  load()
    .then((turnstile) => {
      if (cancelled) return;
      api = turnstile;
      container = document.createElement('div');
      container.setAttribute('aria-hidden', 'true');
      container.style.position = 'fixed';
      container.style.width = '0';
      container.style.height = '0';
      container.style.overflow = 'hidden';
      document.body.appendChild(container);
      widgetId = turnstile.render(container, {
        sitekey: siteKey,
        execution: 'execute',
        appearance: 'interaction-only',
        retry: 'never',
        'refresh-expired': 'manual',
        callback: (token: string) => settle((id) => broker.deliver(id, token)),
        'error-callback': () => {
          settle((id) => broker.fail(id));
          return true;
        },
        'timeout-callback': () => settle((id) => broker.fail(id)),
      });
      const wid = widgetId;
      if (wid === undefined) throw new Error('turnstile_render_failed');
      detach = broker.attach({
        start: (id) => {
          current = id;
          turnstile.reset(wid);
          turnstile.execute(wid);
        },
        reset: () => {
          current = null;
          try {
            turnstile.reset(wid);
          } catch {
            // Best-effort.
          }
        },
      });
      broker.setReady(true);
    })
    .catch(() => {
      if (cancelled) return;
      Sentry.addBreadcrumb({
        category: 'turnstile',
        message: 'Turnstile script unavailable on web',
        level: 'warning',
      });
      detach?.();
      // Answer at once: no token.
      detach = broker.attach({ start: (id) => broker.fail(id), reset: () => {} });
      broker.setReady(true);
    });

  return () => {
    cancelled = true;
    detach?.();
    if (api && widgetId !== undefined) {
      try {
        api.remove(widgetId);
      } catch {
        // Best-effort.
      }
    }
    container?.remove();
  };
}
