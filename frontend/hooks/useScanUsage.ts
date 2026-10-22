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
  /** Enhanced scans this month and the Premium enhanced allowance (0 when not Premium). */
  enhanced_used_month?: number;
  enhanced_monthly_limit?: number;
}

/** Enhanced scans left this month per the usage counters, or null if unknown. */
export function enhancedScansLeft(usage: ScanUsage | null): number | null {
  if (
    !usage ||
    typeof usage.enhanced_monthly_limit !== 'number' ||
    typeof usage.enhanced_used_month !== 'number'
  ) {
    return null;
  }
  return Math.max(0, usage.enhanced_monthly_limit - usage.enhanced_used_month);
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
 * Current user's scan usage, or null while offline or before the first
 * successful load (callers hide the counter then). A failed refetch keeps the
 * previous value; failure is deliberately silent.
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
        if (!cancelled && isScanUsage(res?.data)) setUsage(res.data);
      })
      .catch(() => {
        // Transient failure: keep showing the last known value.
      });
    return () => {
      cancelled = true;
    };
  }, [isConnected, refreshKey]);

  return isConnected ? usage : null;
}
