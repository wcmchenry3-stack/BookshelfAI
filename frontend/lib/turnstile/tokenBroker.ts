import { Sentry } from '../sentry';
import { TURNSTILE_TOKEN_TIMEOUT_MS } from './config';

/**
 * The platform widget a broker drives (#740): the hidden WebView on iOS and
 * Android, the invisible widget on web. It answers a started challenge with
 * `broker.deliver(id, token)` or `broker.fail(id)`.
 */
export interface TurnstileSolver {
  /** Start a fresh challenge for request `id` (reset, then execute). */
  start(id: number): void;
  /** Abandon the current challenge (timed out, aborted or failed). */
  reset(): void;
  /**
   * A request timed out while the widget was not ready (it never loaded, or
   * went away): the widget should recover, e.g. remount its page.
   */
  stalled?(): void;
}

interface PendingRequest {
  id: number;
  resolve: (token: string | null) => void;
  timer: ReturnType<typeof setTimeout>;
  signal?: AbortSignal;
  onAbort?: () => void;
}

type Outcome = 'token' | 'timeout' | 'aborted' | 'error' | 'detached';

/**
 * Hands out one fresh Turnstile token per request, one challenge at a time.
 *
 * - Tokens are single-use and expire after about 300 s, so nothing is cached:
 *   every request runs its own challenge.
 * - A request resolves to null (the scan is then sent without a token) when it
 *   times out (TURNSTILE_TOKEN_TIMEOUT_MS), is aborted (sign-out), or the
 *   widget reports an error. Never rejects.
 * - Each challenge carries the request's id, and a token is accepted only for
 *   the request currently running: a token that arrives after its request
 *   timed out or was aborted is dropped, never given to a later request.
 */
export class TurnstileTokenBroker {
  private solver: TurnstileSolver | null = null;
  private ready = false;
  private queue: PendingRequest[] = [];
  private active: PendingRequest | null = null;
  private nextId = 1;

  /** Register the widget. Returns the detach function. */
  attach(solver: TurnstileSolver): () => void {
    this.solver = solver;
    this.pump();
    return () => {
      if (this.solver !== solver) return;
      this.solver = null;
      this.ready = false;
      // Nothing can answer these any more.
      const waiting = [...(this.active ? [this.active] : []), ...this.queue];
      for (const p of waiting) this.finish(p, null, 'detached');
    };
  }

  /** The widget can (true) or cannot (false) start a challenge now. */
  setReady(ready: boolean): void {
    this.ready = ready;
    if (!ready && this.active) {
      // The page went away mid-challenge (e.g. WebView reload): run it again
      // once the widget is back, within the same timeout.
      this.queue.unshift(this.active);
      this.active = null;
    }
    this.pump();
  }

  /** A fresh token, or null after `timeoutMs`, on abort or on a widget error. */
  request(
    signal?: AbortSignal,
    timeoutMs: number = TURNSTILE_TOKEN_TIMEOUT_MS
  ): Promise<string | null> {
    if (signal?.aborted) return Promise.resolve(null);
    return new Promise<string | null>((resolve) => {
      const p: PendingRequest = {
        id: this.nextId++,
        resolve,
        signal,
        timer: setTimeout(() => this.finish(p, null, 'timeout'), timeoutMs),
      };
      if (signal) {
        p.onAbort = () => this.finish(p, null, 'aborted');
        signal.addEventListener('abort', p.onAbort);
      }
      this.queue.push(p);
      this.pump();
    });
  }

  /** The widget produced `token` for request `id`. Stale ids are ignored. */
  deliver(id: number, token: unknown): void {
    const p = this.active;
    if (!p || p.id !== id) return;
    if (typeof token !== 'string' || token.length === 0 || token.length > 4096) {
      this.finish(p, null, 'error');
      return;
    }
    this.finish(p, token, 'token');
  }

  /** The widget could not produce a token for request `id`. */
  fail(id: number): void {
    const p = this.active;
    if (p && p.id === id) this.finish(p, null, 'error');
  }

  /** The widget failed without naming a request (e.g. it could not render). */
  failCurrent(): void {
    if (this.active) this.finish(this.active, null, 'error');
  }

  /**
   * The widget is broken (its page could not load Turnstile): answer every
   * waiting request with no token now instead of letting each time out.
   */
  failAll(): void {
    const waiting = [...(this.active ? [this.active] : []), ...this.queue];
    for (const p of waiting) this.finish(p, null, 'error');
  }

  private pump(): void {
    if (this.active || !this.solver || !this.ready) return;
    const next = this.queue.shift();
    if (!next) return;
    this.active = next;
    try {
      this.solver.start(next.id);
    } catch {
      this.finish(next, null, 'error');
    }
  }

  private finish(p: PendingRequest, token: string | null, outcome: Outcome): void {
    clearTimeout(p.timer);
    if (p.signal && p.onAbort) p.signal.removeEventListener('abort', p.onAbort);
    // Read before the queue changes: was this request stuck behind a widget
    // that is not ready?
    const stalled = outcome === 'timeout' && !this.ready && this.active !== p;
    if (this.active === p) {
      this.active = null;
      // Throw away whatever the widget is still working on for it.
      if (outcome !== 'token' && this.solver) {
        try {
          this.solver.reset();
        } catch {
          // Best-effort.
        }
      }
    } else {
      const i = this.queue.indexOf(p);
      if (i === -1) return; // Already settled.
      this.queue.splice(i, 1);
    }
    if (outcome !== 'token' && outcome !== 'aborted') {
      // Never the token itself.
      Sentry.addBreadcrumb({
        category: 'turnstile',
        message: `Turnstile token unavailable: ${outcome}`,
        level: 'warning',
      });
    }
    p.resolve(token);
    if (stalled && this.solver?.stalled) {
      try {
        this.solver.stalled();
      } catch {
        // Best-effort.
      }
    }
    this.pump();
  }
}
