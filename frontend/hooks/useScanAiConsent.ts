import { useEffect, useRef, type MutableRefObject } from 'react';

import { AI_CONSENT_ERROR } from '../lib/scanErrors';
import type { ScanJob } from '../lib/scanJob';
import type { ScanAiConsentGate } from '../lib/scan/types';
import { useAiConsent } from './useAiConsent';

/**
 * AI consent for ScanJobProvider (#1023): the gate executeScan reads at call
 * time (a ref, like the Turnstile token getter), and the drain that sends the
 * photos that waited for consent as soon as it is given, whichever screen gave
 * it (the scan prompt, a re-prompt after `ai_consent_required`).
 */
export function useScanAiConsent({
  jobsRef,
  drainingRef,
  drainAgainRef,
  drainQueue,
}: {
  jobsRef: MutableRefObject<ScanJob[]>;
  drainingRef: MutableRefObject<boolean>;
  drainAgainRef: MutableRefObject<boolean>;
  drainQueue: () => Promise<void>;
}): MutableRefObject<ScanAiConsentGate> {
  const { status, checkConsent, ensureConsent, markRequired } = useAiConsent();
  const gateRef = useRef<ScanAiConsentGate>({
    check: checkConsent,
    ensure: () => ensureConsent(),
    markRequired,
  });
  useEffect(() => {
    gateRef.current = { check: checkConsent, ensure: () => ensureConsent(), markRequired };
  }, [checkConsent, ensureConsent, markRequired]);

  useEffect(() => {
    if (status !== 'given') return;
    const waiting = jobsRef.current.some(
      (j) => j.type === 'image' && j.status === 'queued' && j.error === AI_CONSENT_ERROR
    );
    if (!waiting) return;
    // A running drain goes round once more when it ends.
    if (drainingRef.current) drainAgainRef.current = true;
    else void drainQueue();
  }, [status, jobsRef, drainingRef, drainAgainRef, drainQueue]);

  return gateRef;
}
