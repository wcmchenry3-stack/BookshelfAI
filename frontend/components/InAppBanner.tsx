import { useEffect, useRef } from 'react';
import { Animated, Pressable, StyleSheet, Text, View } from 'react-native';

import { useBanner } from '../hooks/useBanner';
import { useTheme } from '../hooks/useTheme';

const SLIDE_DURATION = 300;
const DEFAULT_DISPLAY_DURATION = 5000;

export function InAppBanner() {
  const { banner, hideBanner } = useBanner();
  const { theme } = useTheme();
  const translateY = useRef(new Animated.Value(-120)).current;
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (banner) {
      Animated.timing(translateY, {
        toValue: 0,
        duration: SLIDE_DURATION,
        useNativeDriver: true,
      }).start();

      // A banner that carries an action stays until the user acts or taps it
      // away: a timed-out button is unreachable for a screen-reader user, who
      // may not find it before it disappears.
      if (!banner.actions || banner.actions.length === 0) {
        timerRef.current = setTimeout(() => {
          dismiss();
        }, banner.duration ?? DEFAULT_DISPLAY_DURATION);
      }
    } else {
      translateY.setValue(-120);
    }

    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [banner]);

  function dismiss() {
    if (timerRef.current) clearTimeout(timerRef.current);
    Animated.timing(translateY, {
      toValue: -120,
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
      style={[styles.container, { backgroundColor: bgColor, transform: [{ translateY }] }]}
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
                style={styles.actionButton}
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
    top: 0,
    left: 0,
    right: 0,
    zIndex: 9999,
    paddingTop: 50,
    paddingBottom: 12,
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
    backgroundColor: 'rgba(255,255,255,0.25)',
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
