import { useContext } from 'react';

import { AiConsentContext } from '../contexts/AiConsentContext';

export function useAiConsent() {
  return useContext(AiConsentContext);
}
