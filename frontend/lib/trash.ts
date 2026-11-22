/**
 * The Trash screen (#683): removed library entries, kept 30 days, restorable.
 * The route and navigation helpers live here, as for the scan queue.
 */
type RouterModule = typeof import('expo-router');

function getRouter(): RouterModule['router'] {
  // eslint-disable-next-line @typescript-eslint/no-require-imports -- deliberate lazy require: expo-router is loaded on first navigation, not at import
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

/** Rows with this many days left or fewer show the days-left line in the danger colour. */
export const PURGE_SOON_DAYS = 3;

/**
 * Whole calendar days from `now` to the day `purgeAfter` falls on, both read in
 * the device time zone: 0 on the purge day itself (any time of day), 1 the day
 * before. Counting calendar days (not 24-hour blocks) keeps the number right
 * across a daylight-saving change, when a day is 23 or 25 hours long. A purge
 * date already past reads as 0; an unreadable one is null.
 */
export function daysUntilPurge(purgeAfter: string, now: Date = new Date()): number | null {
  const purge = new Date(purgeAfter);
  if (Number.isNaN(purge.getTime())) return null;
  // Date.UTC of the LOCAL calendar fields is a day count with no DST in it.
  const dayNumber = (d: Date) => Date.UTC(d.getFullYear(), d.getMonth(), d.getDate());
  const days = Math.round((dayNumber(purge) - dayNumber(now)) / 86_400_000);
  return Math.max(0, days);
}

/**
 * Tidies a spoken label whose `{{when}}` part may be empty: drops a sentence
 * stop that is followed by another one, and any trailing stop or space.
 */
export function tidyLabel(label: string): string {
  return label.replace(/([.。।])\s*(?=[.。।])/g, '').replace(/[\s.。।]+$/, '');
}
