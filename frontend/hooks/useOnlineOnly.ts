import { useCallback } from 'react';
import { Alert } from 'react-native';
import { useTranslation } from 'react-i18next';

import { useNetworkStatus } from './useNetworkStatus';

/**
 * Library mutations are server-authoritative and never run offline (scan
 * capture is the only offline feature). Buttons should be disabled while
 * `isConnected` is false; `requireOnline()` is the backstop for a press that
 * still gets through — it returns true when the action may proceed, otherwise
 * tells the user why it can't.
 */
export function useOnlineOnly() {
  const { isConnected } = useNetworkStatus();
  const { t } = useTranslation('common');

  const requireOnline = useCallback((): boolean => {
    if (isConnected) return true;
    Alert.alert(t('offlineTitle'), t('requiresConnection'));
    return false;
  }, [isConnected, t]);

  return { isConnected, requireOnline };
}
