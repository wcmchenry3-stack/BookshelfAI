import { useState } from 'react';
import { Image, Modal, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { MaterialIcons } from '@expo/vector-icons';
import { useTranslation } from 'react-i18next';

import { useTheme } from '../hooks/useTheme';

export interface EnrichedBook {
  book_id?: string;
  open_library_work_id?: string;
  google_books_id?: string;
  title: string;
  author: string;
  description?: string;
  cover_url?: string;
  subjects: string[];
  confidence: number;
  already_in_library: boolean;
  editions: {
    isbn_13?: string;
    isbn_10?: string;
    publisher?: string;
    publish_year?: number;
    page_count?: number;
    format?: string;
  }[];
}

interface Props {
  visible: boolean;
  candidates: EnrichedBook[];
  /** Called with every ticked book when the user confirms. */
  onConfirm: (books: EnrichedBook[]) => void;
  onDismiss: () => void;
  /**
   * Start with every book not already in the library ticked. Right for a photo
   * scan (each result is a different physical book); wrong for a text search,
   * where results are alternative matches for one book.
   */
  preselect?: boolean;
  /** When set, offers a re-scan with the stronger model. */
  onEnhance?: () => void;
  /** Enhanced-scan credits left; null when not yet known. */
  enhancedCredits?: number | null;
}

function initialSelection(candidates: EnrichedBook[], preselect: boolean): Set<number> {
  if (!preselect) return new Set();
  return new Set(candidates.flatMap((book, i) => (book.already_in_library ? [] : [i])));
}

/**
 * Checklist of every book found by a scan or search. Remount (via `key`) to
 * reset the selection when the candidates change.
 */
export function BookCandidatePicker({
  visible,
  candidates,
  onConfirm,
  onDismiss,
  preselect = false,
  onEnhance,
  enhancedCredits = null,
}: Props) {
  const { theme } = useTheme();
  const { t } = useTranslation('components');
  const [selected, setSelected] = useState<Set<number>>(() =>
    initialSelection(candidates, preselect)
  );

  const allSelected = candidates.length > 0 && selected.size === candidates.length;
  const outOfCredits = enhancedCredits === 0;

  function toggle(index: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  }

  function toggleAll() {
    setSelected(allSelected ? new Set() : new Set(candidates.map((_, i) => i)));
  }

  function confirm() {
    onConfirm(candidates.filter((_, i) => selected.has(i)));
  }

  return (
    <Modal
      visible={visible}
      animationType="slide"
      presentationStyle="pageSheet"
      onRequestClose={onDismiss}
      accessibilityViewIsModal
    >
      <View style={[styles.container, { backgroundColor: theme.colors.background }]}>
        <View style={[styles.header, { borderBottomColor: theme.colors.border }]}>
          <Text
            style={[
              styles.title,
              { color: theme.colors.text, fontSize: theme.typography.fontSizeLG },
            ]}
            accessibilityRole="header"
          >
            {t('bookCandidatePicker.title', { count: candidates.length })}
          </Text>
          <Pressable
            onPress={onDismiss}
            accessibilityLabel={t('bookCandidatePicker.closePicker')}
            accessibilityRole="button"
            style={styles.closeButton}
            hitSlop={8}
          >
            <Text style={{ color: theme.colors.primary, fontSize: theme.typography.fontSizeBase }}>
              {t('bookCandidatePicker.cancel')}
            </Text>
          </Pressable>
        </View>

        {candidates.length > 1 && (
          <Pressable
            onPress={toggleAll}
            style={styles.selectAll}
            accessibilityRole="button"
            accessibilityLabel={
              allSelected
                ? t('bookCandidatePicker.clearAllA11y')
                : t('bookCandidatePicker.selectAllA11y')
            }
          >
            <Text style={{ color: theme.colors.primary, fontSize: theme.typography.fontSizeSM }}>
              {allSelected ? t('bookCandidatePicker.clearAll') : t('bookCandidatePicker.selectAll')}
            </Text>
          </Pressable>
        )}

        <ScrollView contentContainerStyle={styles.list}>
          {candidates.map((book, i) => {
            const checked = selected.has(i);
            return (
              <Pressable
                key={`${book.open_library_work_id ?? book.title}-${i}`}
                style={[
                  styles.card,
                  {
                    backgroundColor: theme.colors.surface,
                    borderColor: checked ? theme.colors.primary : theme.colors.border,
                  },
                ]}
                onPress={() => toggle(i)}
                accessibilityRole="checkbox"
                accessibilityState={{ checked }}
                accessibilityLabel={t('bookCandidatePicker.selectBook', {
                  title: book.title,
                  author: book.author,
                })}
              >
                {book.cover_url ? (
                  <Image
                    source={{ uri: book.cover_url }}
                    style={styles.cover}
                    accessibilityLabel={t('coverAlt', { ns: 'common', title: book.title })}
                  />
                ) : (
                  <View
                    style={[
                      styles.cover,
                      styles.coverPlaceholder,
                      { backgroundColor: theme.colors.border },
                    ]}
                    accessibilityLabel={t('noCoverAvailable', { ns: 'common' })}
                  />
                )}

                <View style={styles.info}>
                  <Text
                    style={[
                      styles.bookTitle,
                      { color: theme.colors.text, fontSize: theme.typography.fontSizeBase },
                    ]}
                    numberOfLines={2}
                  >
                    {book.title}
                  </Text>
                  <Text
                    style={[
                      { color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeSM },
                    ]}
                    numberOfLines={1}
                  >
                    {book.author}
                  </Text>
                  {book.editions[0]?.publish_year && (
                    <Text
                      style={[
                        {
                          color: theme.colors.textSecondary,
                          fontSize: theme.typography.fontSizeSM,
                        },
                      ]}
                    >
                      {book.editions[0].publish_year}
                    </Text>
                  )}
                  {book.already_in_library && (
                    <View style={[styles.badge, { backgroundColor: theme.colors.success }]}>
                      <Text style={[styles.badgeText, { color: theme.colors.onSuccess }]}>
                        {t('bookCandidatePicker.alreadyOwned')}
                      </Text>
                    </View>
                  )}
                </View>

                <View style={styles.checkbox}>
                  <MaterialIcons
                    name={checked ? 'check-box' : 'check-box-outline-blank'}
                    size={28}
                    color={checked ? theme.colors.primary : theme.colors.textSecondary}
                  />
                </View>
              </Pressable>
            );
          })}

          {onEnhance && (
            <Pressable
              style={[styles.enhanceButton, { borderColor: theme.colors.border }]}
              onPress={onEnhance}
              disabled={outOfCredits}
              accessibilityRole="button"
              accessibilityLabel={t('bookCandidatePicker.enhanceA11y')}
              accessibilityHint={t('bookCandidatePicker.enhanceHint')}
              accessibilityState={{ disabled: outOfCredits }}
            >
              <Text
                style={{
                  color: outOfCredits ? theme.colors.textSecondary : theme.colors.primary,
                  fontSize: theme.typography.fontSizeBase,
                  fontWeight: '600',
                }}
              >
                {t('bookCandidatePicker.enhance')}
              </Text>
              <Text
                style={{ color: theme.colors.textSecondary, fontSize: theme.typography.fontSizeSM }}
              >
                {outOfCredits
                  ? t('bookCandidatePicker.noCreditsLeft')
                  : enhancedCredits === null
                    ? t('bookCandidatePicker.enhanceCostsCredit')
                    : t('bookCandidatePicker.creditsLeft', { count: enhancedCredits })}
              </Text>
            </Pressable>
          )}
        </ScrollView>

        <View style={[styles.footer, { borderTopColor: theme.colors.border }]}>
          <Pressable
            style={[
              styles.addButton,
              { backgroundColor: theme.colors.primary, opacity: selected.size === 0 ? 0.5 : 1 },
            ]}
            onPress={confirm}
            disabled={selected.size === 0}
            accessibilityRole="button"
            accessibilityState={{ disabled: selected.size === 0 }}
            accessibilityHint={t('bookCandidatePicker.addToWishlistHint')}
          >
            <Text
              style={[
                styles.addButtonText,
                { color: theme.colors.onPrimary, fontSize: theme.typography.fontSizeBase },
              ]}
            >
              {selected.size === 0
                ? t('bookCandidatePicker.addNone')
                : t('bookCandidatePicker.addSelected', { count: selected.size })}
            </Text>
          </Pressable>
        </View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1 },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 16,
    paddingVertical: 16,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  title: { fontWeight: '700' },
  closeButton: { minWidth: 44, minHeight: 44, justifyContent: 'center', alignItems: 'flex-end' },
  selectAll: {
    alignSelf: 'flex-end',
    paddingHorizontal: 16,
    minHeight: 44,
    minWidth: 44,
    justifyContent: 'center',
  },
  list: { padding: 16, paddingTop: 0, gap: 12 },
  card: {
    flexDirection: 'row',
    borderRadius: 8,
    borderWidth: 1,
    overflow: 'hidden',
    minHeight: 44,
  },
  cover: { width: 72, height: 108 },
  coverPlaceholder: { opacity: 0.4 },
  info: { flex: 1, padding: 12, gap: 4 },
  bookTitle: { fontWeight: '600' },
  checkbox: { justifyContent: 'center', paddingHorizontal: 12, minWidth: 44 },
  badge: {
    alignSelf: 'flex-start',
    paddingHorizontal: 8,
    paddingVertical: 2,
    borderRadius: 4,
    marginTop: 4,
  },
  badgeText: { fontSize: 11, fontWeight: '600' },
  enhanceButton: {
    alignItems: 'center',
    paddingVertical: 12,
    borderRadius: 8,
    borderWidth: StyleSheet.hairlineWidth,
    minHeight: 44,
    justifyContent: 'center',
    gap: 2,
  },
  footer: {
    padding: 16,
    borderTopWidth: StyleSheet.hairlineWidth,
  },
  addButton: {
    borderRadius: 8,
    paddingVertical: 14,
    alignItems: 'center',
    minHeight: 48,
    justifyContent: 'center',
  },
  addButtonText: { fontWeight: '600' },
});
