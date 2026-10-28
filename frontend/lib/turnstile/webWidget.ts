import { Sentry } from '../sentry';
import type { TurnstileTokenBroker } from './tokenBroker';
import { loadTurnstileScript, type TurnstileApi } from './webScript';

interface MountedWidget {
  turnstile: TurnstileApi;
  widgetId: string;
}

/**
 * Web (#740): load Cloudflare's script once, render one invisible widget
 * (`execution: 'execute'`) in a zero-size, inert container, and attach it to
 * the broker, which resets and executes it once per /scan attempt. Returns
 * the cleanup.
 *
 * If the script cannot load (offline, blocked), that request is answered with
 * no token at once (the scan is sent without one) and the next request tries
 * the load again, so the widget recovers once the network is back.
 */
export function mountWebTurnstile(
  broker: TurnstileTokenBroker,
  siteKey: string,
  load: () => Promise<TurnstileApi> = loadTurnstileScript
): () => void {
  let cancelled = false;
  let container: HTMLDivElement | null = null;
  let mounted: MountedWidget | null = null;
  // The script load and render in progress, cleared when it fails.
  let mounting: Promise<MountedWidget> | null = null;
  // The broker request the running challenge belongs to.
  let current: number | null = null;

  const settle = (fn: (id: number) => void) => {
    if (current === null) return;
    const id = current;
    current = null;
    fn(id);
  };

  function render(turnstile: TurnstileApi): MountedWidget {
    container = document.createElement('div');
    container.setAttribute('aria-hidden', 'true');
    // Not focusable or reachable, iframe included.
    container.setAttribute('inert', '');
    container.style.position = 'fixed';
    container.style.width = '0';
    container.style.height = '0';
    container.style.overflow = 'hidden';
    document.body.appendChild(container);
    const widgetId = turnstile.render(container, {
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
    if (widgetId === undefined) {
      container.remove();
      container = null;
      throw new Error('turnstile_render_failed');
    }
    return { turnstile, widgetId };
  }

  function ensureWidget(): Promise<MountedWidget> {
    if (mounted) return Promise.resolve(mounted);
    if (mounting) return mounting;
    const attempt = load().then((turnstile) => {
      if (cancelled) throw new Error('turnstile_unmounted');
      mounted = render(turnstile);
      return mounted;
    });
    mounting = attempt;
    attempt.catch(() => {
      if (mounting === attempt) mounting = null;
      if (cancelled) return;
      Sentry.addBreadcrumb({
        category: 'turnstile',
        message: 'Turnstile script unavailable on web',
        level: 'warning',
      });
    });
    return attempt;
  }

  const detach = broker.attach({
    start: (id) => {
      current = id;
      ensureWidget().then(
        (w) => {
          if (current !== id) return; // Timed out or aborted meanwhile.
          try {
            w.turnstile.reset(w.widgetId);
            w.turnstile.execute(w.widgetId);
          } catch {
            settle((rid) => broker.fail(rid));
          }
        },
        // No script: this request goes without a token; the next one retries.
        () => settle((rid) => broker.fail(rid))
      );
    },
    reset: () => {
      current = null;
      if (!mounted) return;
      try {
        mounted.turnstile.reset(mounted.widgetId);
      } catch {
        // Best-effort.
      }
    },
  });
  broker.setReady(true);
  // Load early, so the first scan does not wait for the script.
  ensureWidget().catch(() => {});

  return () => {
    cancelled = true;
    detach();
    if (mounted) {
      try {
        mounted.turnstile.remove(mounted.widgetId);
      } catch {
        // Best-effort.
      }
    }
    container?.remove();
  };
}
