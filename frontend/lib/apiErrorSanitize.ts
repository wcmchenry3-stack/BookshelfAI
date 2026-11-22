/**
 * Capture-safe API error helpers. A LEAF module: it must import neither
 * `./sentry` nor anything that does (lib/sentry.ts -> sentryScrub -> here, and
 * Sentry must self-init before any other module evaluates, rule #13).
 */

export type ApiErrorLike = {
  config?: { method?: string; url?: string };
  response?: { status?: number; data?: { detail?: unknown } | null };
};

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** `/user-books/<uuid>?x=1` -> `/user-books/:id`. No origin, query or IDs. */
export function pathTemplate(url: string | undefined): string {
  if (!url) return 'unknown';
  const noOrigin = url.replace(/^[a-z][a-z0-9+.-]*:\/\/[^/]+/i, '');
  const path = noOrigin.split(/[?#]/)[0] || '/';
  return (
    path
      .split('/')
      // Safe by default: anything that could be an identifier (UUID, ISBN, hash,
      // slug, any segment with a digit, or a long one) becomes `:id`. Static
      // route words (user-books, scan, usage, wishlist, books, search, auth, me,
      // unverified) have no digits and are short, so they stay.
      .map((seg) => (UUID_RE.test(seg) || /\d/.test(seg) || seg.length > 24 ? ':id' : seg))
      .join('/')
  );
}

export function statusOf(err: unknown): number | undefined {
  return (err as ApiErrorLike | null)?.response?.status;
}

/**
 * A capture-safe stand-in for an axios error: name, method, path template and
 * status only. The AxiosError itself carries `config.headers.Authorization`
 * and bodies. Non-axios errors are returned unchanged.
 */
export function sanitizeApiError(err: unknown): unknown {
  const e = err as (ApiErrorLike & { isAxiosError?: boolean }) | null;
  if (!e || typeof e !== 'object' || !(e.isAxiosError || e.config || e.response)) return err;
  const method = (e.config?.method ?? 'get').toUpperCase();
  const status = statusOf(err);
  const out = new Error(
    `API ${method} ${pathTemplate(e.config?.url)} ${status ?? 'no response'}`.trim()
  );
  out.name = 'ApiError';
  return out;
}
