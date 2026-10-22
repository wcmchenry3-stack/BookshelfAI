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

import { BookCandidatePicker } from '../components/BookCandidatePicker';
import { ErrorBoundary } from '../components/ErrorBoundary';
import { InAppBanner } from '../components/InAppBanner';
import { ThemeToggleButton } from '../components/ThemeToggleButton';
import { AuthProvider } from '../contexts/AuthContext';
import { BannerProvider } from '../contexts/BannerContext';
import { ScanJobProvider } from '../contexts/ScanJobContext';
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

function GlobalBookPicker() {
  const { reviewingJob, dismissReview, handleAddBooks, requestEnhancedScan, enhancedCredits } =
    useScanJobs();
  const isPhotoScan = reviewingJob?.type === 'image';
  // Offer the stronger model only on a photo that hasn't already had it.
  const canEnhance = isPhotoScan && !reviewingJob?.enhanced && !!reviewingJob?.imageUri;

  return (
    <BookCandidatePicker
      // Remount per job/result set so the checklist selection resets.
      key={reviewingJob ? `${reviewingJob.id}-${reviewingJob.results?.length ?? 0}` : 'none'}
      visible={reviewingJob !== null}
      candidates={reviewingJob?.results ?? []}
      onConfirm={handleAddBooks}
      onDismiss={dismissReview}
      preselect={isPhotoScan}
      onEnhance={canEnhance ? () => requestEnhancedScan(reviewingJob.id) : undefined}
      enhancedCredits={enhancedCredits}
    />
  );
}

function RootLayout() {
  const [fontsLoaded] = useFonts({
    NotoSerif_700Bold,
    NotoSerif_800ExtraBold,
    Inter_400Regular,
    Inter_500Medium,
    Inter_600SemiBold,
    Inter_700Bold,
  });

  // Hold the splash until fonts are ready; system fonts render if load fails.
  if (!fontsLoaded) {
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
              <GlobalBookPicker />
              <FeedbackButton />
            </ScanJobProvider>
          </AuthProvider>
        </BannerProvider>
      </ThemeProvider>
    </ErrorBoundary>
  );
}

export default Sentry.wrap(RootLayout);
