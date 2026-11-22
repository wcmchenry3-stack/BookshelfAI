/**
 * i18next initialisation for Bookshelf.
 *
 * Performance strategy:
 * - `en` resources are bundled as static imports — always available synchronously
 *   (avoids flash-of-keys on first render and keeps tests working).
 * - All other locales are loaded lazily via i18next-resources-to-backend using
 *   static import() paths that Metro can analyse at build time. Only the active
 *   locale's namespaces are fetched, so 80 of 88 JSON files are deferred.
 *
 * Metro requires static import() paths (no computed template literals), so each
 * locale/namespace pair is listed explicitly as a lazy import function.
 *
 * ar and he are excluded until RTL layout mirroring is implemented.
 * Translation files are preserved in locales/ar/ and locales/he/.
 */
import i18n from 'i18next';
import resourcesToBackend from 'i18next-resources-to-backend';
import { initReactI18next } from 'react-i18next';

import { LOCALES } from './locales';

// ---------------------------------------------------------------------------
// en — static imports (always in the initial bundle)
// ---------------------------------------------------------------------------
import en_common from './locales/en/common.json';
import en_auth from './locales/en/auth.json';
import en_tabs from './locales/en/tabs.json';
import en_myBooks from './locales/en/my-books.json';
import en_scan from './locales/en/scan.json';
import en_settings from './locales/en/settings.json';
import en_wishlist from './locales/en/wishlist.json';
import en_components from './locales/en/components.json';
import en_feedback from './locales/en/feedback.json';

type ImportFn = () => Promise<Record<string, unknown>>;

// ---------------------------------------------------------------------------
// Non-en lazy import map — locale → namespace → () => import(...)
// Metro resolves these at bundle time but executes them only on demand.
// Exported for testing.
// ---------------------------------------------------------------------------
export const importMap: Record<string, Record<string, ImportFn>> = {
  'fr-CA': {
    common: () => import('./locales/fr-CA/common.json'),
    auth: () => import('./locales/fr-CA/auth.json'),
    tabs: () => import('./locales/fr-CA/tabs.json'),
    'my-books': () => import('./locales/fr-CA/my-books.json'),
    scan: () => import('./locales/fr-CA/scan.json'),
    settings: () => import('./locales/fr-CA/settings.json'),
    wishlist: () => import('./locales/fr-CA/wishlist.json'),
    components: () => import('./locales/fr-CA/components.json'),
    feedback: () => import('./locales/fr-CA/feedback.json'),
  },
  es: {
    common: () => import('./locales/es/common.json'),
    auth: () => import('./locales/es/auth.json'),
    tabs: () => import('./locales/es/tabs.json'),
    'my-books': () => import('./locales/es/my-books.json'),
    scan: () => import('./locales/es/scan.json'),
    settings: () => import('./locales/es/settings.json'),
    wishlist: () => import('./locales/es/wishlist.json'),
    components: () => import('./locales/es/components.json'),
    feedback: () => import('./locales/es/feedback.json'),
  },
  hi: {
    common: () => import('./locales/hi/common.json'),
    auth: () => import('./locales/hi/auth.json'),
    tabs: () => import('./locales/hi/tabs.json'),
    'my-books': () => import('./locales/hi/my-books.json'),
    scan: () => import('./locales/hi/scan.json'),
    settings: () => import('./locales/hi/settings.json'),
    wishlist: () => import('./locales/hi/wishlist.json'),
    components: () => import('./locales/hi/components.json'),
    feedback: () => import('./locales/hi/feedback.json'),
  },
  zh: {
    common: () => import('./locales/zh/common.json'),
    auth: () => import('./locales/zh/auth.json'),
    tabs: () => import('./locales/zh/tabs.json'),
    'my-books': () => import('./locales/zh/my-books.json'),
    scan: () => import('./locales/zh/scan.json'),
    settings: () => import('./locales/zh/settings.json'),
    wishlist: () => import('./locales/zh/wishlist.json'),
    components: () => import('./locales/zh/components.json'),
    feedback: () => import('./locales/zh/feedback.json'),
  },
  ja: {
    common: () => import('./locales/ja/common.json'),
    auth: () => import('./locales/ja/auth.json'),
    tabs: () => import('./locales/ja/tabs.json'),
    'my-books': () => import('./locales/ja/my-books.json'),
    scan: () => import('./locales/ja/scan.json'),
    settings: () => import('./locales/ja/settings.json'),
    wishlist: () => import('./locales/ja/wishlist.json'),
    components: () => import('./locales/ja/components.json'),
    feedback: () => import('./locales/ja/feedback.json'),
  },
  ko: {
    common: () => import('./locales/ko/common.json'),
    auth: () => import('./locales/ko/auth.json'),
    tabs: () => import('./locales/ko/tabs.json'),
    'my-books': () => import('./locales/ko/my-books.json'),
    scan: () => import('./locales/ko/scan.json'),
    settings: () => import('./locales/ko/settings.json'),
    wishlist: () => import('./locales/ko/wishlist.json'),
    components: () => import('./locales/ko/components.json'),
    feedback: () => import('./locales/ko/feedback.json'),
  },
  pt: {
    common: () => import('./locales/pt/common.json'),
    auth: () => import('./locales/pt/auth.json'),
    tabs: () => import('./locales/pt/tabs.json'),
    'my-books': () => import('./locales/pt/my-books.json'),
    scan: () => import('./locales/pt/scan.json'),
    settings: () => import('./locales/pt/settings.json'),
    wishlist: () => import('./locales/pt/wishlist.json'),
    components: () => import('./locales/pt/components.json'),
    feedback: () => import('./locales/pt/feedback.json'),
  },
  de: {
    common: () => import('./locales/de/common.json'),
    auth: () => import('./locales/de/auth.json'),
    tabs: () => import('./locales/de/tabs.json'),
    'my-books': () => import('./locales/de/my-books.json'),
    scan: () => import('./locales/de/scan.json'),
    settings: () => import('./locales/de/settings.json'),
    wishlist: () => import('./locales/de/wishlist.json'),
    components: () => import('./locales/de/components.json'),
    feedback: () => import('./locales/de/feedback.json'),
  },
  nl: {
    common: () => import('./locales/nl/common.json'),
    auth: () => import('./locales/nl/auth.json'),
    tabs: () => import('./locales/nl/tabs.json'),
    'my-books': () => import('./locales/nl/my-books.json'),
    scan: () => import('./locales/nl/scan.json'),
    settings: () => import('./locales/nl/settings.json'),
    wishlist: () => import('./locales/nl/wishlist.json'),
    components: () => import('./locales/nl/components.json'),
    feedback: () => import('./locales/nl/feedback.json'),
  },
  ru: {
    common: () => import('./locales/ru/common.json'),
    auth: () => import('./locales/ru/auth.json'),
    tabs: () => import('./locales/ru/tabs.json'),
    'my-books': () => import('./locales/ru/my-books.json'),
    scan: () => import('./locales/ru/scan.json'),
    settings: () => import('./locales/ru/settings.json'),
    wishlist: () => import('./locales/ru/wishlist.json'),
    components: () => import('./locales/ru/components.json'),
    feedback: () => import('./locales/ru/feedback.json'),
  },
};

i18n
  .use(
    resourcesToBackend((language: string, namespace: string) => {
      const loader = importMap[language]?.[namespace];
      if (!loader) return Promise.reject(new Error(`No translations for ${language}/${namespace}`));
      return loader();
    })
  )
  .use(initReactI18next)
  .init({
    resources: {
      en: {
        common: en_common,
        auth: en_auth,
        tabs: en_tabs,
        'my-books': en_myBooks,
        scan: en_scan,
        settings: en_settings,
        wishlist: en_wishlist,
        components: en_components,
        feedback: en_feedback,
      },
    },
    lng: 'en',
    fallbackLng: 'en',
    supportedLngs: LOCALES.map((l) => l.code),
    ns: [
      'common',
      'auth',
      'tabs',
      'my-books',
      'scan',
      'settings',
      'wishlist',
      'components',
      'feedback',
    ],
    defaultNS: 'common',
    interpolation: {
      escapeValue: false,
    },
    react: {
      useSuspense: false,
    },
  })
  // Prevent unhandled promise rejection — fatal in React Native.
  .catch((err: unknown) => console.error('[i18n] init failed:', err));
