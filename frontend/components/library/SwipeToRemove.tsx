import { useCallback, useEffect, useMemo, useRef } from 'react';
import { I18nManager, Pressable, StyleSheet } from 'react-native';
import { MaterialIcons } from '@expo/vector-icons';
import * as Haptics from 'expo-haptics';
import ReanimatedSwipeable, {
  type SwipeableMethods,
} from 'react-native-gesture-handler/ReanimatedSwipeable';
import {
  ReduceMotion,
  useAnimatedReaction,
  useReducedMotion,
  type SharedValue,
} from 'react-native-reanimated';
import { scheduleOnRN } from 'react-native-worklets';

import { AppText } from '../ui/AppText';
import { useTheme } from '../../hooks/useTheme';
import type { SwipeRowHandle, SwipeToRemoveProps } from './swipeToRemoveShared';

/** Width of the revealed Remove panel (mockup: 88px). */
export const REMOVE_PANEL_WIDTH = 88;
/** Released past half the panel, the row stays open on the panel. */
const OPEN_THRESHOLD = REMOVE_PANEL_WIDTH / 2;
/**
 * Released past twice the panel's width (a full swipe), the book is removed
 * straight away. Crossing it gives a haptic tick.
 */
export const FULL_SWIPE_DISTANCE = REMOVE_PANEL_WIDTH * 2;
/**
 * Horizontal travel before the swipe takes the touch (the pan's `activeOffsetX`,
 * which ReanimatedSwipeable sets from `dragOffsetFromLeft/Right`). Larger than
 * the default 10 so a vertical scroll or pull to refresh that drifts sideways
 * stays a scroll.
 */
export const SWIPE_ACTIVATION_OFFSET = 20;

/** No spring overshoot ever; with Reduce Motion on, no spring at all. */
export const SPRING_OPTIONS = { overshootClamping: true };
export const REDUCED_MOTION_OPTIONS = {
  overshootClamping: true,
  reduceMotion: ReduceMotion.Always,
};

interface RemovePanelProps {
  translation: SharedValue<number>;
  /** -1 when the panel is on the right (LTR), 1 when on the left (RTL). */
  side: -1 | 1;
  onArmedChange: (armed: boolean) => void;
  onPress: () => void;
  label: string;
  text: string;
}

/** The red Remove button behind the row; reports when a drag passes the full-swipe distance. */
function RemovePanel({ translation, side, onArmedChange, onPress, label, text }: RemovePanelProps) {
  const { theme } = useTheme();
  useAnimatedReaction(
    () => translation.value * side >= FULL_SWIPE_DISTANCE,
    (armed, previous) => {
      // `previous` is null on the first run, which can already be mid-drag.
      if (armed !== (previous ?? false)) scheduleOnRN(onArmedChange, armed);
    },
    [side, onArmedChange]
  );
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={label}
      style={[styles.panel, { backgroundColor: theme.colors.danger }]}
      testID="swipe-remove-panel"
    >
      <MaterialIcons name="delete-outline" size={24} color={theme.colors.onDanger} />
      <AppText variant="meta" color="onDanger" numberOfLines={1}>
        {text}
      </AppText>
    </Pressable>
  );
}

/**
 * Swipe a row towards the start edge to reveal Remove (#935): a short swipe
 * leaves the row open on the panel, a full swipe removes straight away. iOS
 * and Android only; `SwipeToRemove.web.tsx` renders the row without a gesture.
 * Mirrored in RTL (the panel is on the left). Screen readers do not swipe: the
 * row exposes a `remove` accessibility action instead (see `LibraryRow`).
 */
export function SwipeToRemove({
  children,
  onRemove,
  label,
  actionText,
  disabled = false,
  group,
}: SwipeToRemoveProps) {
  const { theme } = useTheme();
  const reduceMotion = useReducedMotion();
  const rowRef = useRef<SwipeableMethods>(null);
  const armedRef = useRef(false);
  const handle = useMemo<SwipeRowHandle>(() => ({ close: () => rowRef.current?.close() }), []);

  const remove = useCallback(async () => {
    try {
      await onRemove();
    } catch {
      // `onRemove` shows its own error alert; nothing to add here.
    } finally {
      // A removed row unmounts; any other outcome springs back.
      rowRef.current?.close();
    }
  }, [onRemove]);

  const onArmedChange = useCallback((armed: boolean) => {
    armedRef.current = armed;
    if (armed) Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium).catch(() => {});
  }, []);

  const onWillOpen = useCallback(() => {
    group?.onOpen(handle);
    if (!armedRef.current) return;
    armedRef.current = false;
    void remove();
  }, [group, handle, remove]);

  const onClose = useCallback(() => group?.onClose(handle), [group, handle]);

  useEffect(() => {
    if (disabled) rowRef.current?.close();
  }, [disabled]);

  const rtl = I18nManager.isRTL;
  const renderActions = useCallback(
    (_progress: SharedValue<number>, translation: SharedValue<number>) => (
      <RemovePanel
        translation={translation}
        side={rtl ? 1 : -1}
        onArmedChange={onArmedChange}
        onPress={() => void remove()}
        label={label}
        text={actionText}
      />
    ),
    [rtl, onArmedChange, remove, label, actionText]
  );
  const sideProps = rtl
    ? { renderLeftActions: renderActions, leftThreshold: OPEN_THRESHOLD }
    : { renderRightActions: renderActions, rightThreshold: OPEN_THRESHOLD };

  return (
    <ReanimatedSwipeable
      ref={rowRef}
      enabled={!disabled}
      dragOffsetFromLeft={SWIPE_ACTIVATION_OFFSET}
      dragOffsetFromRight={-SWIPE_ACTIVATION_OFFSET}
      animationOptions={reduceMotion ? REDUCED_MOTION_OPTIONS : SPRING_OPTIONS}
      onSwipeableWillOpen={onWillOpen}
      onSwipeableClose={onClose}
      childrenContainerStyle={{ backgroundColor: theme.colors.background }}
      testID="swipe-to-remove"
      {...sideProps}
    >
      {children}
    </ReanimatedSwipeable>
  );
}

const styles = StyleSheet.create({
  panel: {
    width: REMOVE_PANEL_WIDTH,
    alignItems: 'center',
    justifyContent: 'center',
    gap: 4,
    paddingHorizontal: 4,
  },
});
