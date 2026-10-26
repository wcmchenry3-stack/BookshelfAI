import { useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Modal,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { useTranslation } from 'react-i18next';

import { useTheme } from '../hooks/useTheme';
import { api } from '../lib/api';
import {
  UNVERIFIED_LIMITS,
  unverifiedFailure,
  type UnverifiedDraft,
  type UnverifiedFailure,
} from '../lib/unverified';

const FAILURE_KEYS: Record<UnverifiedFailure, string> = {
  duplicate: 'addUnverified.duplicate',
  limit: 'addUnverified.limitReached',
  isbn: 'addUnverified.isbnInvalid',
  invalid: 'addUnverified.invalid',
  failed: 'addUnverified.failed',
};

export interface SavedUnverifiedBook {
  id: string;
  unverified: true;
  book: { id: string; title: string; author: string };
}

interface Props {
  visible: boolean;
  /** Starting values, from what the scan read. */
  draft: UnverifiedDraft;
  /**
   * Identifies the draft. When it changes the fields reset to the new draft
   * in place: the Modal stays mounted, because iOS can fail to present a page
   * sheet in the same commit that unmounts another (#616).
   */
  draftKey: string;
  /** Called with the new library entry once POST /wishlist/unverified succeeds. */
  onSaved: (entry: SavedUnverifiedBook) => void;
  onDismiss: () => void;
  /** The sheet has finished dismissing (iOS only): the Modal's onDismiss. */
  onHidden?: () => void;
}

/**
 * "Add it yourself" (#616): a short form, pre-filled from the scan, that saves
 * a book the catalog could not find as the user's own unverified entry.
 */
export function AddUnverifiedBookSheet({
  visible,
  draft,
  draftKey,
  onSaved,
  onDismiss,
  onHidden,
}: Props) {
  const { theme } = useTheme();
  const { colors } = theme;
  const { t } = useTranslation('components');
  const [title, setTitle] = useState(draft.title);
  const [author, setAuthor] = useState(draft.author);
  const [isbn, setIsbn] = useState(draft.isbn);
  const [titleError, setTitleError] = useState('');
  const [authorError, setAuthorError] = useState('');
  const [formError, setFormError] = useState('');
  const [saving, setSaving] = useState(false);
  const [shownKey, setShownKey] = useState(draftKey);
  const draftKeyRef = useRef(draftKey);
  useEffect(() => {
    draftKeyRef.current = draftKey;
  }, [draftKey]);
  if (draftKey !== shownKey) {
    setShownKey(draftKey);
    setSaving(false);
    setTitle(draft.title);
    setAuthor(draft.author);
    setIsbn(draft.isbn);
    setTitleError('');
    setAuthorError('');
    setFormError('');
  }

  async function save() {
    if (saving) return;
    const cleanTitle = title.trim();
    const cleanAuthor = author.trim();
    setTitleError(cleanTitle ? '' : t('addUnverified.titleRequired'));
    setAuthorError(cleanAuthor ? '' : t('addUnverified.authorRequired'));
    setFormError('');
    if (!cleanTitle || !cleanAuthor) return;
    setSaving(true);
    const startedFor = draftKey;
    try {
      const { data } = await api.post<SavedUnverifiedBook>('/wishlist/unverified', {
        title: cleanTitle,
        author: cleanAuthor,
        isbn: isbn.trim() || null,
        // Which scan candidate the form was pre-filled from (#631); left
        // out for a form with no scan behind it.
        ...(draft.scanReference ? { scan_reference: draft.scanReference } : {}),
      });
      // onSaved is this render's callback, bound to the draft being saved.
      onSaved(data);
    } catch (err) {
      // Show the failure only on the form it belongs to.
      if (draftKeyRef.current === startedFor) {
        setFormError(t(FAILURE_KEYS[unverifiedFailure(err)]));
      }
    } finally {
      if (draftKeyRef.current === startedFor) setSaving(false);
    }
  }

  const inputStyle = (hasError: boolean) => [
    styles.input,
    {
      color: colors.onSurface,
      backgroundColor: colors.surfaceContainerHigh,
      borderColor: hasError ? colors.error : colors.outline,
      fontSize: theme.typography.fontSizeBase,
    },
  ];

  return (
    <Modal
      visible={visible}
      animationType="slide"
      presentationStyle="pageSheet"
      onRequestClose={onDismiss}
      onDismiss={onHidden}
      accessibilityViewIsModal
    >
      <View style={[styles.container, { backgroundColor: colors.background }]}>
        <View style={[styles.header, { borderBottomColor: colors.border }]}>
          <Text
            style={[styles.heading, { color: colors.text, fontSize: theme.typography.fontSizeLG }]}
            accessibilityRole="header"
          >
            {t('addUnverified.title')}
          </Text>
          <Pressable
            onPress={onDismiss}
            accessibilityRole="button"
            accessibilityLabel={t('addUnverified.closeA11y')}
            style={styles.headerButton}
            hitSlop={8}
          >
            <Text style={{ color: colors.primary, fontSize: theme.typography.fontSizeBase }}>
              {t('addUnverified.cancel')}
            </Text>
          </Pressable>
        </View>

        <ScrollView contentContainerStyle={styles.body} keyboardShouldPersistTaps="handled">
          <Text style={{ color: colors.textSecondary, fontSize: theme.typography.fontSizeSM }}>
            {t('addUnverified.intro')}
          </Text>

          <Text style={[styles.label, { color: colors.onSurface }]} nativeID="unverified-title">
            {t('addUnverified.titleLabel')}
          </Text>
          <TextInput
            testID="unverified-title-input"
            style={inputStyle(!!titleError)}
            value={title}
            onChangeText={(v) => {
              setTitle(v);
              if (titleError) setTitleError('');
            }}
            maxLength={UNVERIFIED_LIMITS.title}
            accessibilityLabel={t('addUnverified.titleLabel')}
            accessibilityLabelledBy="unverified-title"
            returnKeyType="next"
          />
          {titleError ? (
            <Text style={[styles.fieldError, { color: colors.error }]} accessibilityRole="alert">
              {titleError}
            </Text>
          ) : null}

          <Text style={[styles.label, { color: colors.onSurface }]} nativeID="unverified-author">
            {t('addUnverified.authorLabel')}
          </Text>
          <TextInput
            testID="unverified-author-input"
            style={inputStyle(!!authorError)}
            value={author}
            onChangeText={(v) => {
              setAuthor(v);
              if (authorError) setAuthorError('');
            }}
            maxLength={UNVERIFIED_LIMITS.author}
            accessibilityLabel={t('addUnverified.authorLabel')}
            accessibilityLabelledBy="unverified-author"
            returnKeyType="next"
          />
          {authorError ? (
            <Text style={[styles.fieldError, { color: colors.error }]} accessibilityRole="alert">
              {authorError}
            </Text>
          ) : null}

          <Text style={[styles.label, { color: colors.onSurface }]} nativeID="unverified-isbn">
            {t('addUnverified.isbnLabel')}
          </Text>
          <TextInput
            testID="unverified-isbn-input"
            style={inputStyle(false)}
            value={isbn}
            onChangeText={setIsbn}
            maxLength={UNVERIFIED_LIMITS.isbn}
            keyboardType="numbers-and-punctuation"
            autoCapitalize="characters"
            autoCorrect={false}
            accessibilityLabel={t('addUnverified.isbnLabel')}
            accessibilityLabelledBy="unverified-isbn"
          />

          {formError ? (
            <Text
              style={[styles.formError, { color: colors.error }]}
              accessibilityRole="alert"
              accessibilityLiveRegion="polite"
            >
              {formError}
            </Text>
          ) : null}
        </ScrollView>

        <View style={[styles.footer, { borderTopColor: colors.border }]}>
          <Pressable
            style={[styles.saveButton, { backgroundColor: colors.primary }, saving && styles.busy]}
            onPress={save}
            disabled={saving}
            accessibilityRole="button"
            accessibilityLabel={t('addUnverified.save')}
            accessibilityHint={t('addUnverified.saveHint')}
            accessibilityState={{ disabled: saving, busy: saving }}
          >
            {saving ? (
              <ActivityIndicator color={colors.onPrimary} />
            ) : (
              <Text
                style={[
                  styles.saveText,
                  { color: colors.onPrimary, fontSize: theme.typography.fontSizeBase },
                ]}
              >
                {t('addUnverified.save')}
              </Text>
            )}
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
  heading: { fontWeight: '700' },
  headerButton: { minWidth: 44, minHeight: 44, justifyContent: 'center', alignItems: 'flex-end' },
  body: { padding: 16, gap: 8 },
  label: { fontWeight: '600', marginTop: 8 },
  input: {
    borderWidth: 1,
    borderRadius: 8,
    paddingHorizontal: 12,
    paddingVertical: 10,
    minHeight: 44,
  },
  fieldError: { fontSize: 13 },
  formError: { fontSize: 14, fontWeight: '600', marginTop: 8 },
  footer: { padding: 16, borderTopWidth: StyleSheet.hairlineWidth },
  saveButton: {
    borderRadius: 8,
    paddingVertical: 14,
    alignItems: 'center',
    justifyContent: 'center',
    minHeight: 48,
  },
  busy: { opacity: 0.6 },
  saveText: { fontWeight: '600' },
});
