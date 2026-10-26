import { useEffect, useState } from 'react';
import { ActivityIndicator, Alert, Image, Pressable, StyleSheet, Text, View } from 'react-native';
import * as Google from 'expo-auth-session/providers/google';
import * as WebBrowser from 'expo-web-browser';
import { useTranslation } from 'react-i18next';

import { useAuth } from '../../hooks/useAuth';
import { useTheme } from '../../hooks/useTheme';
import { api } from '../../lib/api';

WebBrowser.maybeCompleteAuthSession();

const showTestLogin = __DEV__ && process.env.EXPO_PUBLIC_ENVIRONMENT !== 'production';

export default function LoginScreen() {
  const { theme } = useTheme();
  const { login, testLogin } = useAuth();
  const { t } = useTranslation('auth');

  const [testLoading, setTestLoading] = useState(false);
  const [promptFailed, setPromptFailed] = useState(false);

  const [request, response, promptAsync] = Google.useIdTokenAuthRequest({
    webClientId: process.env.EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID,
    iosClientId: process.env.EXPO_PUBLIC_GOOGLE_IOS_CLIENT_ID,
    androidClientId: process.env.EXPO_PUBLIC_GOOGLE_ANDROID_CLIENT_ID,
  });

  const googleError = promptFailed || response?.type === 'error';

  useEffect(() => {
    if (response?.type === 'success') {
      const { id_token } = response.params;
      login(id_token).catch(() => {
        Alert.alert(t('errorTitle', { ns: 'common' }), t('signInError'));
      });
    }
  }, [response, login, t]);

  // promptAsync can reject (blocked popup, network failure); never let that
  // escape as an unhandled rejection (#660).
  const handleGoogleSignIn = async () => {
    setPromptFailed(false);
    try {
      await promptAsync();
    } catch {
      setPromptFailed(true);
    }
  };

  const handleTestLogin = async () => {
    setTestLoading(true);
    try {
      const { data } = await api.post('/auth/test-login', {
        secret: process.env.EXPO_PUBLIC_TEST_AUTH_SECRET ?? '',
        email: process.env.EXPO_PUBLIC_TEST_EMAIL ?? '',
      });
      await testLogin(data.access_token, data.refresh_token);
    } catch {
      Alert.alert('Test Login Failed', 'Check TEST_AUTH_SECRET and backend config.');
    } finally {
      setTestLoading(false);
    }
  };

  return (
    <View style={[styles.container, { backgroundColor: theme.colors.surface }]}>
      {/* Brand mark */}
      <Image
        source={require('../../assets/logo.png')}
        style={styles.logo}
        resizeMode="contain"
        accessibilityLabel="BookshelfAI"
        accessibilityRole="image"
      />

      <Text
        style={[
          styles.title,
          { color: theme.colors.primary, fontFamily: theme.typography.fontFamilyHeadline },
        ]}
        accessibilityRole="header"
      >
        {t('appTitle')}
      </Text>

      <Text style={[styles.subtitle, { color: theme.colors.onSurfaceVariant }]}>
        {t('signInPrompt')}
      </Text>

      {/* Google sign-in */}
      <Pressable
        style={[
          styles.button,
          {
            backgroundColor: theme.colors.onSurface,
            marginTop: theme.spacing.xl,
            opacity: request ? 1 : 0.6,
          },
        ]}
        onPress={handleGoogleSignIn}
        disabled={!request}
        accessibilityLabel={t('signIn')}
        accessibilityRole="button"
        accessibilityHint={t('signInHint')}
      >
        {!request ? (
          <ActivityIndicator color={theme.colors.surface} />
        ) : (
          <Text style={[styles.buttonText, { color: theme.colors.surface }]}>{t('signIn')}</Text>
        )}
      </Pressable>

      {googleError && (
        <View style={styles.errorBox}>
          <Text
            style={[styles.errorText, { color: theme.colors.error }]}
            accessibilityRole="alert"
            accessibilityLiveRegion="polite"
          >
            {t('signInUnavailable')}
          </Text>
          <Pressable
            onPress={handleGoogleSignIn}
            disabled={!request}
            style={styles.retryButton}
            accessibilityLabel={t('signInRetry')}
            accessibilityRole="button"
          >
            <Text style={[styles.retryText, { color: theme.colors.primary }]}>
              {t('signInRetry')}
            </Text>
          </Pressable>
        </View>
      )}

      {showTestLogin && (
        <Pressable
          style={[
            styles.button,
            {
              backgroundColor: theme.colors.secondaryContainer,
              marginTop: theme.spacing.md,
            },
          ]}
          onPress={handleTestLogin}
          disabled={testLoading}
          accessibilityLabel="Test Login"
          accessibilityRole="button"
          accessibilityHint="Dev-only: bypasses Google OAuth for E2E testing"
        >
          {testLoading ? (
            <ActivityIndicator color={theme.colors.onSecondaryContainer} />
          ) : (
            <Text style={[styles.buttonText, { color: theme.colors.onSecondaryContainer }]}>
              Test Login
            </Text>
          )}
        </Pressable>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: 32,
  },
  logo: {
    width: 120,
    height: 120,
    borderRadius: 24,
    marginBottom: 24,
  },
  title: {
    fontSize: 32,
    fontWeight: '700',
    letterSpacing: -0.5,
    textAlign: 'center',
  },
  subtitle: {
    fontSize: 16,
    textAlign: 'center',
    marginTop: 8,
    lineHeight: 24,
  },
  button: {
    paddingHorizontal: 24,
    paddingVertical: 14,
    borderRadius: 12,
    minWidth: 240,
    minHeight: 44,
    alignItems: 'center',
    justifyContent: 'center',
  },
  errorBox: {
    alignItems: 'center',
    marginTop: 16,
    maxWidth: 320,
  },
  errorText: {
    fontSize: 14,
    textAlign: 'center',
    lineHeight: 20,
  },
  retryButton: {
    minHeight: 44,
    paddingHorizontal: 16,
    justifyContent: 'center',
  },
  retryText: {
    fontWeight: '600',
    fontSize: 16,
  },
  buttonText: {
    fontWeight: '600',
    fontSize: 16,
  },
});
