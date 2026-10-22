import { useEffect, useState } from 'react';

import { api } from '../lib/api';
import { useNetworkStatus } from './useNetworkStatus';

/** Body of GET /scan/usage. Limits come from the server; never hardcode them. */
export interface ScanUsage {
  used_today: number;
  daily_limit: number;
  used_month: number;
  monthly_limit: number;
  /** First day of next month (UTC), YYYY-MM-DD. */
  period_end: string;
}

function isScanUsage(v: unknown): v is ScanUsage {
  const u = v as Partial<ScanUsage> | null;
  return (
    !!u &&
    typeof u.used_today === 'number' &&
    typeof u.daily_limit === 'number' &&
    typeof u.used_month === 'number' &&
    typeof u.monthly_limit === 'number'
  );
}

/**
 * Current user's scan usage, or null while loading, offline, or if the request
 * fails (callers hide the counter then; failure is deliberately silent).
 * Bump `refreshKey` to refetch, e.g. after a scan completes.
 */
export function useScanUsage(refreshKey = 0): ScanUsage | null {
  const { isConnected } = useNetworkStatus();
  const [usage, setUsage] = useState<ScanUsage | null>(null);

  useEffect(() => {
    if (!isConnected) return;
    let cancelled = false;
    api
      .get('/scan/usage')
      .then((res) => {
        if (!cancelled) setUsage(isScanUsage(res?.data) ? res.data : null);
      })
      .catch(() => {
        if (!cancelled) setUsage(null);
      });
    return () => {
      cancelled = true;
    };
  }, [isConnected, refreshKey]);

  return isConnected ? usage : null;
}
