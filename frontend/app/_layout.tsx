// Sentry MUST be the first import — its module self-initialises so the
// native crash reporter is ready before any other module can throw.
import { Sentry } from '../lib/sentry';

import '../src/i18n/i18n';

import { View } from 'react-native';
import { Stack } from 'expo-router';
import { useFonts } from 'expo-font';
import { NotoSerif_600SemiBold, NotoSerif_700Bold } from '@expo-google-fonts/noto-serif';
import {
  Inter_400Regular,
  Inter_500Medium,
  Inter_600SemiBold,
  Inter_700Bold,
} from '@expo-google-fonts/inter';

import { AddUnverifiedBookSheet } from '../components/AddUnverifiedBookSheet';
import { BookCandidatePicker } from '../components/BookCandidatePicker';
import { ErrorBoundary } from '../components/ErrorBoundary';
import { InAppBanner } from '../components/InAppBanner';
import { AuthProvider } from '../contexts/AuthContext';
import { BannerProvider } from '../contexts/BannerContext';
import { ScanJobProvider } from '../contexts/ScanJobContext';
import { TurnstileProvider } from '../contexts/TurnstileContext';
import { useModalHandoff } from '../hooks/useModalHandoff';
import { useScanJobs } from '../hooks/useScanJobs';
import { useTheme } from '../hooks/useTheme';
import { ThemeProvider } from '../theme';
import { FeedbackProvider } from '../components/FeedbackWidget/FeedbackContext';
import { SessionLogger } from '../components/FeedbackWidget/SessionLogger';

// ES imports are hoisted, so every import above (Sentry first) has evaluated before
// this runs. Kept below the imports so `import/first` passes without a disable.
// Record that the root layout module has evaluated — helps diagnose
// crashes that happen between Sentry init and first render.
SessionLogger.init();
Sentry.addBreadcrumb({
  category: 'app.lifecycle',
  message: 'Root layout module evaluated',
  level: 'info',
});

// Separate component so useTheme() can be called inside ThemeProvider.
function InnerStack() {
  const { theme } = useTheme();
  // The tab screens have no header (#936): each owns its top safe area and its
  // own title. Theme and help live in Settings. The header that remains is the
  // default one on stack screens pushed over the tabs (trash, scan queue), opaque
  // in both themes (#906) with no blur and no shadow; #932 restyles it.
  return (
    <Stack
      screenOptions={{
        headerStyle: { backgroundColor: theme.colors.background },
        // The back arrow follows the theme (the default tint is near-black in dark mode).
        headerTintColor: theme.colors.primary,
        headerShadowVisible: false,
        headerTransparent: false,
      }}
    >
      {/* No app chrome on sign-in (the route is "(auth)/login": there is no (auth)/_layout); also keeps the raw route name out of the title (#908). */}
      <Stack.Screen name="(auth)/login" options={{ headerShown: false }} />
      <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
      <Stack.Screen name="scan-queue" options={{ title: '', headerTransparent: false }} />
      <Stack.Screen name="trash" options={{ title: '', headerTransparent: false }} />
    </Stack>
  );
}

/**
 * The review picker and the "Add it yourself" form (#616), never on screen at
 * once. Each waits for the other to finish dismissing before it presents:
 * iOS can fail to present a page sheet in the commit that unmounts another
 * Modal (see useModalHandoff).
 */
function GlobalScanModals() {
  const {
    reviewingJob,
    dismissReview,
    handleAddBooks,
    requestEnhancedScan,
    enhancedScansRemaining,
    addYourself,
    unverifiedDrafts,
    unverifiedSaved,
    dismissUnverified,
  } = useScanJobs();
  const draft = unverifiedDrafts?.[0];
  // The sheet waits for the picker; the picker waits for the sheet.
  const pickerGate = useModalHandoff(reviewingJob !== null);
  const sheetGate = useModalHandoff(!!draft);

  const isPhotoScan = reviewingJob?.type === 'image';
  // Offer the stronger model only on a photo that hasn't already had it, and
  // only when the server says enhanced scans are left (Premium-only, #533).
  const canEnhance =
    isPhotoScan &&
    !reviewingJob?.enhanced &&
    !!reviewingJob?.imageUri &&
    enhancedScansRemaining !== null &&
    enhancedScansRemaining > 0;

  return (
    <>
      <BookCandidatePicker
        // Remount per job/result set so the checklist selection resets.
        key={reviewingJob ? `${reviewingJob.id}-${reviewingJob.results?.length ?? 0}` : 'none'}
        visible={reviewingJob !== null && sheetGate.clear}
        candidates={reviewingJob?.results ?? []}
        onConfirm={handleAddBooks}
        onDismiss={dismissReview}
        onHidden={pickerGate.notifyDismissed}
        preselect={isPhotoScan}
        onEnhance={canEnhance ? () => requestEnhancedScan(reviewingJob.id) : undefined}
        enhancedScansRemaining={enhancedScansRemaining}
        // Closes the picker without discarding the scan's other candidates.
        onAddYourself={(book) => addYourself([book])}
      />
      {draft && pickerGate.clear ? (
        <AddUnverifiedBookSheet
          // One Modal for the whole queue: the next draft resets the fields
          // in place rather than remounting (and re-presenting) the sheet.
          draftKey={draft.id}
          visible
          draft={draft}
          // Bound to this render's draft: a save that finishes after its form
          // was closed completes that draft, not whichever is shown by then.
          onSaved={(entry) => unverifiedSaved(entry.book.title, draft.id)}
          onDismiss={dismissUnverified}
          onHidden={sheetGate.notifyDismissed}
        />
      ) : null}
    </>
  );
}

function RootLayout() {
  // One font file per weight (theme/typography.ts `fontFiles`): Android ignores
  // fontWeight on custom fonts, so every weight a variant uses is loaded here.
  const [fontsLoaded, fontsError] = useFonts({
    NotoSerif_600SemiBold,
    NotoSerif_700Bold,
    Inter_400Regular,
    Inter_500Medium,
    Inter_600SemiBold,
    Inter_700Bold,
  });

  // Hold the splash until fonts are ready; if loading fails (blocked font
  // request, 12s timeout on web) fall through and render with system fonts.
  if (!fontsLoaded && !fontsError) {
    return <View />;
  }

  return (
    <ErrorBoundary>
      <ThemeProvider>
        <BannerProvider>
          <AuthProvider>
            {/* One hidden Turnstile widget for every POST /scan (#740). */}
            <TurnstileProvider>
              <ScanJobProvider>
                <FeedbackProvider>
                  <InAppBanner />
                  <InnerStack />
                  <GlobalScanModals />
                </FeedbackProvider>
              </ScanJobProvider>
            </TurnstileProvider>
          </AuthProvider>
        </BannerProvider>
      </ThemeProvider>
    </ErrorBoundary>
  );
}

export default Sentry.wrap(RootLayout);
