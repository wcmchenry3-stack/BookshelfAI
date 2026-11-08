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
 * failed), it shows Scan (the default) as an unconfirmed guess and re-reads
 * the value from the server once on mount. `choose` is optimistic: the new
 * value shows at once, and if the save fails the old value comes back and an
 * error banner shows. Picking the value already shown is a no-op only when
 * the server confirmed it; an unconfirmed pick is saved.
 *
 * The value lives here, not in AuthContext: the app only routes on it at
 * sign-in and launch, and both read it fresh from `GET /auth/me`.
 */
export function useDefaultTabSetting() {
  const { defaultTab: signedInTab } = useAuth();
  const { showBanner } = useBanner();
  const { t } = useTranslation('settings');
  const [defaultTab, setDefaultTab] = useState<DefaultTab>(() => parseDefaultTab(signedInTab));
  // False while the shown value is only the Scan fallback for an unknown one.
  const [confirmed, setConfirmed] = useState(signedInTab !== null);
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
      if (tab && !cancelled && !choseRef.current) {
        setDefaultTab(tab);
        setConfirmed(true);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [needsReread]);

  const choose = useCallback(
    async (tab: DefaultTab) => {
      if (savingRef.current || (confirmed && tab === defaultTab)) return;
      choseRef.current = true;
      savingRef.current = true;
      setSaving(true);
      const previous = defaultTab;
      setDefaultTab(tab);
      try {
        setDefaultTab(await saveDefaultTab(tab));
        setConfirmed(true);
      } catch (err) {
        setDefaultTab(previous);
        // The choice did not take; let a pending re-read show the server's value.
        choseRef.current = false;
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
    [confirmed, defaultTab, showBanner, t]
  );

  return { defaultTab, choose, saving };
}
