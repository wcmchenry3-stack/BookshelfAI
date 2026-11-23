import { useCallback, useEffect, useMemo, useRef } from 'react';
import { I18nManager, Pressable, StyleSheet, View } from 'react-native';
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
  /** Gives the row the swipeable's translation, read when the swipe is let go. */
  onTranslation: (translation: SharedValue<number>) => void;
  onArmedChange: (armed: boolean) => void;
  onPress: () => void;
  disabled: boolean;
  label: string;
  text: string;
}

/**
 * The red Remove button behind the row. It is hidden from screen readers:
 * gesture-handler keeps it mounted behind a closed row, where VoiceOver and
 * TalkBack would otherwise focus an invisible button. They remove through the
 * row's `remove` action instead. The haptic tick fires as a drag crosses the
 * full-swipe distance.
 */
function RemovePanel({
  translation,
  side,
  onTranslation,
  onArmedChange,
  onPress,
  disabled,
  label,
  text,
}: RemovePanelProps) {
  const { theme } = useTheme();
  useEffect(() => onTranslation(translation), [onTranslation, translation]);
  useAnimatedReaction(
    () => translation.value * side >= FULL_SWIPE_DISTANCE,
    (armed, previous) => {
      // `previous` is null on the first run, which can already be mid-drag.
      if (armed !== (previous ?? false)) scheduleOnRN(onArmedChange, armed);
    },
    [side, onArmedChange]
  );
  return (
    <View
      style={styles.panelWrap}
      accessibilityElementsHidden
      importantForAccessibility="no-hide-descendants"
      testID="swipe-remove-panel-wrap"
    >
      <Pressable
        onPress={onPress}
        disabled={disabled}
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
    </View>
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
  const translationRef = useRef<SharedValue<number> | null>(null);
  const removingRef = useRef(false);
  const handle = useMemo<SwipeRowHandle>(() => ({ close: () => rowRef.current?.close() }), []);
  const side = I18nManager.isRTL ? 1 : -1;

  const remove = useCallback(async () => {
    if (removingRef.current) return;
    removingRef.current = true;
    let removed = false;
    try {
      removed = await onRemove();
    } catch {
      // `onRemove` shows its own error alert; nothing to add here.
    } finally {
      removingRef.current = false;
    }
    // A removed row unmounts as it is: closing it would flash. Any other
    // outcome (failure, offline, a 412) springs back.
    if (!removed) rowRef.current?.close();
  }, [onRemove]);

  const onTranslation = useCallback((translation: SharedValue<number>) => {
    translationRef.current = translation;
  }, []);

  // Haptic tick only. The reaction runs a frame late, so it never decides removal.
  const onArmedChange = useCallback((armed: boolean) => {
    if (armed) Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Medium).catch(() => {});
  }, []);

  // Decided only from where the row is when the swipe is let go. The haptic
  // reaction runs a frame later, so trusting it would both miss a same-frame
  // flick and remove a book the user dragged back under the distance.
  const onWillOpen = useCallback(() => {
    group?.onOpen(handle);
    const travelled = (translationRef.current?.value ?? 0) * side;
    if (travelled >= FULL_SWIPE_DISTANCE) void remove();
  }, [group, handle, remove, side]);

  const onClose = useCallback(() => group?.onClose(handle), [group, handle]);

  // Offline, or another change to this book in flight: close an open row,
  // unless it is this row's own removal.
  useEffect(() => {
    if (disabled && !removingRef.current) rowRef.current?.close();
  }, [disabled]);

  const renderActions = useCallback(
    (_progress: SharedValue<number>, translation: SharedValue<number>) => (
      <RemovePanel
        translation={translation}
        side={side}
        onTranslation={onTranslation}
        onArmedChange={onArmedChange}
        onPress={() => void remove()}
        disabled={disabled}
        label={label}
        text={actionText}
      />
    ),
    [side, onTranslation, onArmedChange, remove, disabled, label, actionText]
  );
  const sideProps =
    side === 1
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
  panelWrap: { flexDirection: 'row' },
  panel: {
    width: REMOVE_PANEL_WIDTH,
    alignItems: 'center',
    justifyContent: 'center',
    gap: 4,
    paddingHorizontal: 4,
  },
});
