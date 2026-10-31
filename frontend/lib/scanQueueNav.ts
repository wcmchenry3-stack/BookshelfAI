// Navigation helpers for the scan queue screen (#605). expo-router is loaded
// on first use, not at import: the scan job context and the scan screen are
// imported by many modules and tests that never navigate.
type RouterModule = typeof import('expo-router');

function getRouter(): RouterModule['router'] {
  // eslint-disable-next-line @typescript-eslint/no-require-imports -- deliberate lazy require: expo-router is loaded on first navigation, not at import
  return (require('expo-router') as RouterModule).router;
}

const SCAN_QUEUE_ROUTE = '/scan-queue';

export function openScanQueue(): void {
  getRouter().push(SCAN_QUEUE_ROUTE);
}

export function leaveScanQueue(): void {
  const router = getRouter();
  if (router.canGoBack()) router.back();
  else router.replace('/');
}
