import { TURNSTILE_ORIGIN, TURNSTILE_SCRIPT_URL } from './config';

/** The bridge object the inline page puts on `window` for injected calls. */
export const TURNSTILE_BRIDGE = '__bookshelfTurnstile';

/** Messages the inline page posts to React Native. */
export type TurnstilePageMessage =
  | { type: 'ready' }
  | { type: 'token'; id: number; token: string }
  | { type: 'error'; id: number | null; code: string };

/** Parse a WebView message; anything that is not one of ours is null. */
export function parseTurnstileMessage(data: unknown): TurnstilePageMessage | null {
  if (typeof data !== 'string' || data.length > 8192) return null;
  let msg: unknown;
  try {
    msg = JSON.parse(data);
  } catch {
    return null;
  }
  if (!msg || typeof msg !== 'object') return null;
  const m = msg as Record<string, unknown>;
  if (m.type === 'ready') return { type: 'ready' };
  if (m.type === 'token' && typeof m.id === 'number' && typeof m.token === 'string') {
    return { type: 'token', id: m.id, token: m.token };
  }
  if (m.type === 'error' && (typeof m.id === 'number' || m.id === null)) {
    return { type: 'error', id: m.id as number | null, code: String(m.code ?? '') };
  }
  return null;
}

/** JS to start a challenge for request `id` in the page. */
export function runChallengeScript(id: number): string {
  return `window.${TURNSTILE_BRIDGE}&&window.${TURNSTILE_BRIDGE}.run(${Math.trunc(id)});true;`;
}

/** JS to abandon the current challenge in the page. */
export function resetChallengeScript(): string {
  return `window.${TURNSTILE_BRIDGE}&&window.${TURNSTILE_BRIDGE}.reset();true;`;
}

/**
 * The inline page for the hidden WebView (#740): one invisible widget,
 * rendered with `execution: 'execute'` so a challenge runs only when React
 * Native asks for one (`run(id)`), and reset before each run so every /scan
 * attempt gets a fresh single-use token. `siteKey` must already be validated
 * (config.turnstileSiteKey); it is JSON-encoded here as well.
 */
export function buildTurnstileHtml(siteKey: string): string {
  const key = JSON.stringify(siteKey).replace(/</g, '\\u003c');
  return `<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' ${TURNSTILE_ORIGIN}; frame-src ${TURNSTILE_ORIGIN}; connect-src ${TURNSTILE_ORIGIN}; style-src 'unsafe-inline'">
</head><body><div id="cf-widget"></div>
<script>
(function () {
  var SITE_KEY = ${key};
  var widgetId = null;
  var current = null;
  var pending = null;
  function post(m) {
    if (window.ReactNativeWebView) window.ReactNativeWebView.postMessage(JSON.stringify(m));
  }
  function settle(m) {
    if (current === null) return;
    m.id = current;
    current = null;
    post(m);
  }
  function run(id) {
    current = id;
    if (widgetId === null) { pending = id; return; }
    try {
      window.turnstile.reset(widgetId);
      window.turnstile.execute(widgetId);
    } catch (e) {
      settle({ type: 'error', code: 'execute' });
    }
  }
  function reset() {
    current = null;
    pending = null;
    if (widgetId === null) return;
    try { window.turnstile.reset(widgetId); } catch (e) {}
  }
  window.${TURNSTILE_BRIDGE} = { run: run, reset: reset };
  window.onBookshelfTurnstileLoad = function () {
    try {
      widgetId = window.turnstile.render('#cf-widget', {
        sitekey: SITE_KEY,
        execution: 'execute',
        appearance: 'interaction-only',
        retry: 'never',
        'refresh-expired': 'manual',
        callback: function (token) { settle({ type: 'token', token: token }); },
        'error-callback': function (code) { settle({ type: 'error', code: String(code) }); return true; },
        'timeout-callback': function () { settle({ type: 'error', code: 'timeout' }); }
      });
    } catch (e) {
      post({ type: 'error', id: current, code: 'render' });
      return;
    }
    post({ type: 'ready' });
    if (pending !== null) { var p = pending; pending = null; run(p); }
  };
})();
</script>
<script src="${TURNSTILE_SCRIPT_URL}&onload=onBookshelfTurnstileLoad" async defer></script>
</body></html>`;
}
