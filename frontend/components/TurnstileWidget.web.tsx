import { useEffect } from 'react';

import { mountWebTurnstile } from '../lib/turnstile/webWidget';
import type { TurnstileWidgetProps } from './TurnstileWidget';

/** Web: an invisible Turnstile widget outside the React tree (#740). No UI. */
export function TurnstileWidget({ broker, siteKey }: TurnstileWidgetProps) {
  useEffect(() => mountWebTurnstile(broker, siteKey), [broker, siteKey]);
  return null;
}
