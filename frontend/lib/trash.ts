/**
 * The Trash screen (#683): removed library entries, kept 30 days, restorable.
 * The route and navigation helpers live here, as for the scan queue.
 */
type RouterModule = typeof import('expo-router');

function getRouter(): RouterModule['router'] {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  return (require('expo-router') as RouterModule).router;
}

export const TRASH_ROUTE = '/trash';

export function openTrash(): void {
  getRouter().push(TRASH_ROUTE);
}

export function leaveTrash(): void {
  const router = getRouter();
  if (router.canGoBack()) router.back();
  else router.replace('/');
}

/** Why POST /user-books/{id}/restore failed, as the screen reacts to it. */
export type RestoreFailure = 'conflict' | 'limit' | 'not_found' | 'unavailable' | 'failed';

export function restoreFailure(reason: unknown): RestoreFailure {
  const response = (reason as { response?: { status?: number; data?: { detail?: unknown } } })
    ?.response;
  const detail = response?.data?.detail;
  if (response?.status === 404) return 'not_found';
  if (response?.status === 409) {
    if (detail === 'restore_conflict') return 'conflict';
    if (detail === 'unverified_limit_reached') return 'limit';
    return 'failed';
  }
  if (response?.status === 403 && detail === 'forbidden') return 'unavailable';
  return 'failed';
}

/** A date in the user's local time zone and locale. */
export function formatLocalDate(iso: string, locale: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  try {
    return d.toLocaleDateString(locale, { dateStyle: 'medium' });
  } catch {
    return d.toLocaleDateString();
  }
}
