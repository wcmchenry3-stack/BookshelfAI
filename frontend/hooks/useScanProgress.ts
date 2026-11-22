import { useEffect, useRef } from 'react';
import { AccessibilityInfo } from 'react-native';
import { useTranslation } from 'react-i18next';

import type { ScanJob } from '../lib/scanJob';

// Jobs created before this (restored from a previous launch) are never shown
// as being sent until they really are ('searching').
const LAUNCHED_AT = Date.now();

// A 'pending' job with no scheduled retry, created since this launch, is
// being sent right now (connectivity check). One restored from a previous
// launch (interrupted mid-scan) is waiting to be sent, not in progress.
const isStarting = (j: ScanJob) =>
  j.status === 'pending' && j.nextAttemptAt == null && j.createdAt >= LAUNCHED_AT;

/**
 * Derives the Scan screen's queue counts and progress state, and announces
 * the start of a scan on iOS (accessibilityLiveRegion is Android-only).
 *
 * A scan is in progress (#742) from the shutter press (photo being saved)
 * through job status 'searching' until the result or an error arrives. The
 * client can't see the server's phase: one honest message, no percentage.
 */
export function useScanProgress(jobs: ScanJob[], pendingCount: number, capturing: boolean) {
  const { t } = useTranslation('scan');
  const startingCount = jobs.filter(isStarting).length;
  // Jobs being sent right now are shown as progress, not as waiting.
  const waitingCount = Math.max(0, pendingCount - startingCount);
  const failedCount = jobs.filter((j) => j.status === 'failed').length;
  const imageInProgress =
    capturing || jobs.some((j) => (j.status === 'searching' || isStarting(j)) && j.type !== 'text');
  const inProgress = imageInProgress || jobs.some((j) => j.status === 'searching');
  const progressText = t(imageInProgress ? 'scanInProgress' : 'searchInProgress');
  const wasInProgress = useRef(false);
  useEffect(() => {
    if (inProgress && !wasInProgress.current) {
      AccessibilityInfo.announceForAccessibility(progressText);
    }
    wasInProgress.current = inProgress;
  }, [inProgress, progressText]);
  return { waitingCount, failedCount, inProgress, progressText };
}
