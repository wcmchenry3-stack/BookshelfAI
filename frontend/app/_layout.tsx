// Sentry MUST be the first import — its module self-initialises so the
// native crash reporter is ready before any other module can throw.
import { Sentry } from '../lib/sentry';

// Record that the root layout module has evaluated — helps diagnose
// crashes that happen between Sentry init and first render.
SessionLogger.init();
Sentry.addBreadcrumb({
  category: 'app.lifecycle',
  message: 'Root layout module evaluated',
  level: 'info',
});

import '../src/i18n/i18n';

import { Image, Platform, StyleSheet, Text, View } from 'react-native';
import { Stack } from 'expo-router';
import { useFonts } from 'expo-font';
import { NotoSerif_700Bold, NotoSerif_800ExtraBold } from '@expo-google-fonts/noto-serif';
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
import { ThemeToggleButton } from '../components/ThemeToggleButton';
import { AuthProvider } from '../contexts/AuthContext';
import { BannerProvider } from '../contexts/BannerContext';
import { ScanJobProvider } from '../contexts/ScanJobContext';
import { useModalHandoff } from '../hooks/useModalHandoff';
import { useScanJobs } from '../hooks/useScanJobs';
import { useTheme } from '../hooks/useTheme';
import { ThemeProvider } from '../theme';
import { FeedbackButton } from '../components/FeedbackWidget/FeedbackButton';
import { SessionLogger } from '../components/FeedbackWidget/SessionLogger';

function HeaderLogo() {
  const { theme } = useTheme();
  return (
    <View style={headerStyles.row}>
      <Image
        source={require('../assets/icon.png')}
        style={headerStyles.logo}
        resizeMode="contain"
        accessibilityLabel="BookshelfAI"
        accessibilityRole="image"
      />
      <Text style={[headerStyles.wordmark, { color: theme.colors.primary }]}>BookshelfAI</Text>
    </View>
  );
}

// Separate component so useTheme() can be called inside ThemeProvider.
function InnerStack() {
  const { theme } = useTheme();
  // Dark mode: semi-transparent frosted-glass header; light mode: transparent.
  const headerBg = theme.isDark ? 'rgba(17,19,23,0.7)' : 'transparent';
  const webStyle = Platform.OS === 'web' && theme.isDark ? { backdropFilter: 'blur(20px)' } : {};

  return (
    <Stack
      screenOptions={{
        headerLeft: () => <HeaderLogo />,
        headerRight: () => <ThemeToggleButton />,
        headerStyle: { backgroundColor: headerBg, ...webStyle },
        headerShadowVisible: false,
        headerTransparent: theme.isDark,
      }}
    >
      <Stack.Screen name="(tabs)" options={{ title: '' }} />
      {/* Opaque header: the list starts below it, not under the dark-mode glass header. */}
      <Stack.Screen name="scan-queue" options={{ title: '', headerTransparent: false }} />
    </Stack>
  );
}

const headerStyles = StyleSheet.create({
  row: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  logo: { width: 40, height: 40, borderRadius: 8 },
  wordmark: {
    fontSize: 20,
    fontFamily: 'NotoSerif_700Bold',
    fontWeight: '700',
    letterSpacing: -0.3,
  },
});

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
  const [fontsLoaded, fontsError] = useFonts({
    NotoSerif_700Bold,
    NotoSerif_800ExtraBold,
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
            <ScanJobProvider>
              <InAppBanner />
              <InnerStack />
              <GlobalScanModals />
              <FeedbackButton />
            </ScanJobProvider>
          </AuthProvider>
        </BannerProvider>
      </ThemeProvider>
    </ErrorBoundary>
  );
}

export default Sentry.wrap(RootLayout);
