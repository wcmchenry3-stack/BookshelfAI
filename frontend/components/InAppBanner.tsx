import { useEffect, useRef } from 'react';
import { Animated, Platform, Pressable, StyleSheet, Text, View } from 'react-native';

import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { useBanner } from '../hooks/useBanner';
import { useTheme } from '../hooks/useTheme';

const SLIDE_DURATION = 300;
const DEFAULT_DISPLAY_DURATION = 5000;
// A result banner that carries an action (e.g. "Book found" + View) stays a bit
// longer so the action is reachable, but never indefinitely (#909).
const ACTION_DISPLAY_DURATION = 10000;
const SIDE_MARGIN = 16;
const GAP_BELOW_HEADER = 8;
const SLIDE_DISTANCE = 240;

/** Stack header height below the safe-area inset: 44 iOS, 56 Android, 64 web. */
function headerContentHeight(): number {
  return Platform.OS === 'android' ? 56 : Platform.OS === 'web' ? 64 : 44;
}

export function InAppBanner() {
  const { banner, hideBanner } = useBanner();
  const { theme } = useTheme();
  const insets = useSafeAreaInsets();
  const translateY = useRef(new Animated.Value(-SLIDE_DISTANCE)).current;
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (banner) {
      Animated.timing(translateY, {
        toValue: 0,
        duration: SLIDE_DURATION,
        useNativeDriver: true,
      }).start();

      // An error that carries an action (Retry, "Add it yourself") stays until
      // the user acts or taps it away: a timed-out button is unreachable for a
      // screen-reader user. Every other banner, including a result banner with
      // a "View" action, times out so it cannot linger across tabs (#909).
      const sticky = banner.type === 'error' && !!banner.actions && banner.actions.length > 0;
      if (!sticky) {
        const hasActions = !!banner.actions && banner.actions.length > 0;
        const fallback = hasActions ? ACTION_DISPLAY_DURATION : DEFAULT_DISPLAY_DURATION;
        timerRef.current = setTimeout(() => {
          dismiss();
        }, banner.duration ?? fallback);
      }
    } else {
      translateY.setValue(-SLIDE_DISTANCE);
    }

    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dismiss is re-created every render; the effect must re-run only when the banner changes
  }, [banner]);

  function dismiss() {
    if (timerRef.current) clearTimeout(timerRef.current);
    Animated.timing(translateY, {
      toValue: -SLIDE_DISTANCE,
      duration: SLIDE_DURATION,
      useNativeDriver: true,
    }).start(() => hideBanner());
  }

  if (!banner) return null;

  const bgColor =
    banner.type === 'success'
      ? theme.colors.success
      : banner.type === 'error'
        ? theme.colors.error
        : theme.colors.primary;

  const textColor =
    banner.type === 'success'
      ? theme.colors.onSuccess
      : banner.type === 'error'
        ? theme.colors.onError
        : theme.colors.onPrimary;

  return (
    <Animated.View
      style={[
        styles.container,
        {
          top: insets.top + headerContentHeight() + GAP_BELOW_HEADER,
          backgroundColor: bgColor,
          transform: [{ translateY }],
        },
      ]}
      testID="in-app-banner"
      accessibilityRole="alert"
      accessibilityLiveRegion="assertive"
    >
      <Pressable onPress={dismiss} style={styles.content}>
        <Text
          style={[styles.message, { color: textColor }]}
          numberOfLines={banner.numberOfLines ?? 2}
        >
          {banner.message}
        </Text>
        {banner.actions && banner.actions.length > 0 && (
          <View style={styles.actions}>
            {banner.actions.map((action) => (
              <Pressable
                key={action.label}
                onPress={() => {
                  action.onPress();
                  dismiss();
                }}
                style={[styles.actionButton, { borderColor: textColor }]}
                accessibilityRole="button"
                accessibilityLabel={action.label}
              >
                <Text style={[styles.actionText, { color: textColor }]}>{action.label}</Text>
              </Pressable>
            ))}
          </View>
        )}
      </Pressable>
    </Animated.View>
  );
}

const styles = StyleSheet.create({
  container: {
    position: 'absolute',
    left: SIDE_MARGIN,
    right: SIDE_MARGIN,
    zIndex: 9999,
    borderRadius: 16,
    elevation: 6,
    paddingVertical: 12,
    paddingHorizontal: 16,
  },
  content: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 12,
  },
  message: {
    flex: 1,
    fontWeight: '600',
    fontSize: 14,
  },
  actions: {
    flexDirection: 'row',
    gap: 8,
  },
  actionButton: {
    paddingHorizontal: 12,
    paddingVertical: 6,
    borderRadius: 4,
    borderWidth: 1,
    minWidth: 44,
    minHeight: 44,
    alignItems: 'center',
    justifyContent: 'center',
  },
  actionText: {
    fontWeight: '700',
    fontSize: 13,
  },
});
