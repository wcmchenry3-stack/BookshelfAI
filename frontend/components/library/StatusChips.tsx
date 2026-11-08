import { useCallback, useEffect, useRef } from 'react';
import { ScrollView, StyleSheet, View } from 'react-native';
import { useTranslation } from 'react-i18next';

import { Chip } from '../ui/Chip';
import { SCREEN_PADDING, STATUS_TAB_KEYS, type Status } from './libraryShared';

interface StatusChipsProps {
  activeTab: Status;
  counts: Record<Status, number>;
  onChange: (tab: Status) => void;
}

/** Horizontal status filter chips (All / Wishlist / Purchased / ...) with a count each. */
export function StatusChips({ activeTab, counts, onChange }: StatusChipsProps) {
  // Reads the `my-books` namespace (My Books only).
  const { t } = useTranslation('my-books');
  const scrollRef = useRef<ScrollView>(null);
  const slots = useRef<Partial<Record<Status, number>>>({});
  const revealed = useRef(false);

  // Brings a chip's leading edge (less the content padding) to the start of the
  // viewport; the native scroll clamps it, so the last chip ends fully in view.
  const reveal = useCallback((key: Status) => {
    const x = slots.current[key];
    if (x === undefined) return;
    scrollRef.current?.scrollTo({
      x: Math.max(0, x - SCREEN_PADDING),
      animated: revealed.current,
    });
    revealed.current = true;
  }, []);

  useEffect(() => reveal(activeTab), [activeTab, reveal]);

  return (
    <ScrollView
      ref={scrollRef}
      horizontal
      showsHorizontalScrollIndicator={false}
      style={styles.scroll}
      testID="status-tabs"
      contentContainerStyle={styles.content}
    >
      {STATUS_TAB_KEYS.map((key) => {
        const label = t(`statusTab.${key}`);
        const count = counts[key];
        return (
          <View
            key={key}
            testID={`status-chip-slot-${key}`}
            onLayout={(e) => {
              slots.current[key] = e.nativeEvent.layout.x;
              if (key === activeTab) reveal(key);
            }}
          >
            <Chip
              label={label}
              count={count}
              selected={activeTab === key}
              onPress={() => onChange(key)}
              accessibilityLabel={t('filterBy', { label, count })}
              testID={`status-chip-${key}`}
            />
          </View>
        );
      })}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  // Never takes flexible space from the list/empty state (#905): without
  // flexGrow/flexShrink 0 a horizontal ScrollView in a flex column collapses
  // beside a FlatList and stretches when the list is empty. No fixed height:
  // the 44px (min) chips grow with large text instead of clipping.
  scroll: { flexGrow: 0, flexShrink: 0 },
  // Leading and trailing padding live in the content, so the last chip scrolls fully into view.
  content: {
    paddingHorizontal: SCREEN_PADDING,
    paddingTop: 12,
    paddingBottom: 8,
    gap: 8,
    alignItems: 'center',
  },
});
