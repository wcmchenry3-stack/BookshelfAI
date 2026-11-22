import { TURNSTILE_SCRIPT_URL } from './config';

/** The part of Cloudflare's `window.turnstile` API the web widget uses. */
export interface TurnstileApi {
  render(container: HTMLElement | string, options: Record<string, unknown>): string | undefined;
  execute(widgetId: string): void;
  reset(widgetId: string): void;
  remove(widgetId: string): void;
}

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

let loading: Promise<TurnstileApi> | null = null;

/**
 * Load Cloudflare's Turnstile script once (web, #740). Later calls share the
 * same promise; after a failure the next call tries again.
 */
export function loadTurnstileScript(): Promise<TurnstileApi> {
  if (typeof window !== 'undefined' && window.turnstile) return Promise.resolve(window.turnstile);
  if (loading) return loading;
  const attempt = new Promise<TurnstileApi>((resolve, reject) => {
    const script = document.createElement('script');
    script.src = TURNSTILE_SCRIPT_URL;
    script.async = true;
    script.defer = true;
    script.onload = () =>
      window.turnstile ? resolve(window.turnstile) : reject(new Error('turnstile_unavailable'));
    script.onerror = () => reject(new Error('turnstile_script_failed'));
    document.head.appendChild(script);
  });
  loading = attempt;
  attempt.catch(() => {
    if (loading === attempt) loading = null;
  });
  return attempt;
}

/** Tests only: forget the shared load. */
export function resetTurnstileScriptForTests(): void {
  loading = null;
}
