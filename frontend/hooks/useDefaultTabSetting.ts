import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { useAuth } from './useAuth';
import { useBanner } from './useBanner';
import { toApiError } from '../lib/apiError';
import {
  type DefaultTab,
  loadDefaultTab,
  parseDefaultTab,
  saveDefaultTab,
} from '../lib/defaultTab';
import { Sentry } from '../lib/sentry';

/**
 * The Settings "Open on" value (#913). Starts from the tab AuthContext read at
 * sign-in or launch; when that is unknown (null: launch was offline, the read
 * failed), it re-reads it from the server once on mount. `choose` is optimistic: the new value shows at once,
 * and if the save fails the old value comes back and an error banner shows.
 *
 * The value lives here, not in AuthContext: the app only routes on it at
 * sign-in and launch, and both read it fresh from `GET /auth/me`.
 */
export function useDefaultTabSetting() {
  const { defaultTab: signedInTab } = useAuth();
  const { showBanner } = useBanner();
  const { t } = useTranslation('settings');
  const [defaultTab, setDefaultTab] = useState<DefaultTab>(() => parseDefaultTab(signedInTab));
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  // Once the user has chosen, a late answer from the mount re-read must not
  // overwrite their choice.
  const choseRef = useRef(false);

  // Re-read only when sign-in or launch could not read it (AuthContext has
  // null): otherwise the value is already the server's.
  const needsReread = signedInTab === null;
  useEffect(() => {
    if (!needsReread) return;
    let cancelled = false;
    void loadDefaultTab().then((tab) => {
      if (tab && !cancelled && !choseRef.current) setDefaultTab(tab);
    });
    return () => {
      cancelled = true;
    };
  }, [needsReread]);

  const choose = useCallback(
    async (tab: DefaultTab) => {
      if (savingRef.current || tab === defaultTab) return;
      choseRef.current = true;
      savingRef.current = true;
      setSaving(true);
      const previous = defaultTab;
      setDefaultTab(tab);
      try {
        setDefaultTab(await saveDefaultTab(tab));
      } catch (err) {
        setDefaultTab(previous);
        const { status, code } = toApiError(err);
        Sentry.addBreadcrumb({
          category: 'settings',
          message: 'open-on save failed',
          level: 'warning',
          data: { status, code },
        });
        showBanner({ message: t('openOnSaveFailed'), type: 'error' });
      } finally {
        savingRef.current = false;
        setSaving(false);
      }
    },
    [defaultTab, showBanner, t]
  );

  return { defaultTab, choose, saving };
}
