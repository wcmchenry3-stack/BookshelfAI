import React, { memo, useCallback, useState } from 'react';
import { Image, StyleSheet, Text, View, type StyleProp, type ViewStyle } from 'react-native';
import { useTranslation } from 'react-i18next';

import { useTheme } from '../../hooks/useTheme';
import { tokens } from '../../theme/tokens';
import { longestUnitEm } from '../../theme/coverTextWidths';
import { fontFor } from '../../theme/typography';

export type BookCoverSize = 'xs' | 'sm' | 'md' | 'lg';

/** Fixed 2:3 frames (#902). The height never comes from a parent. */
export const COVER_SIZES: Record<BookCoverSize, { width: number; height: number }> = {
  xs: { width: 48, height: 72 },
  sm: { width: 52, height: 78 },
  md: { width: 104, height: 156 },
  lg: { width: 136, height: 204 },
};

/** Title type scale for the generated cover, per size. */
const FALLBACK_TEXT: Record<
  BookCoverSize,
  { fontSize: number; lineHeight: number; lines: number }
> = {
  xs: { fontSize: 8, lineHeight: 10, lines: 5 },
  sm: { fontSize: 9, lineHeight: 11, lines: 6 },
  md: { fontSize: 14, lineHeight: 18, lines: 6 },
  lg: { fontSize: 17, lineHeight: 22, lines: 6 },
};

/** Smallest title font the generated cover uses; below it the cover shows no title. */
export const FALLBACK_MIN_FONT = 6;

export interface FallbackTitleStyle {
  /** The title to draw; empty when `placeholder` is set. */
  text: string;
  fontSize: number;
  lineHeight: number;
  lines: number;
  /** Width of the text box; the text is centred inside the frame. */
  width: number;
  /** True when no word fits at the floor: the cover is drawn with no title. */
  placeholder: boolean;
}

/** Inset of the thin inner frame; the text box keeps 1px more air on each side. */
function fallbackInset(frameWidth: number): number {
  return Math.max(3, Math.round(frameWidth * 0.07));
}

const halfDown = (n: number) => Math.floor(n * 2) / 2;

/**
 * Deterministic title sizing for the generated cover (#924). A word should not
 * break mid-word, so the font is chosen from the widest unbreakable unit,
 * measured with the real Noto Serif SemiBold advances (`theme/coverTextWidths`)
 * plus a 4% margin. The size is the largest that fits, capped at the size's
 * normal font and floored at `FALLBACK_MIN_FONT`. If the widest unit still does
 * not fit at the floor (a long single word on a small cover), the cover is drawn
 * as a plain placeholder with no title, rather than splitting the word. CJK text wraps per character, so it keeps the normal size.
 * Widths are estimates: a different font fallback can still differ slightly.
 */
export function fallbackTitleStyle(title: string, size: BookCoverSize): FallbackTitleStyle {
  const base = FALLBACK_TEXT[size];
  const frameWidth = COVER_SIZES[size].width;
  const width = frameWidth - 2 * (fallbackInset(frameWidth) + 1);
  const ratio = base.lineHeight / base.fontSize;
  const fitted = halfDown(width / Math.max(longestUnitEm(title), 0.01));
  if (fitted >= FALLBACK_MIN_FONT) {
    const fontSize = Math.min(base.fontSize, fitted);
    return {
      text: title,
      fontSize,
      lineHeight: Math.round(fontSize * ratio),
      lines: base.lines,
      width,
      placeholder: false,
    };
  }
  return { ...base, text: '', width, placeholder: true };
}

export interface BookCoverProps {
  /** The book's title, drawn on the generated cover. */
  title: string;
  /** The cover image URL; null or undefined draws the generated cover. */
  coverUrl?: string | null;
  size: BookCoverSize;
  /**
   * Overrides the default label (`common:coverAlt`, or `common:noCoverAvailable`
   * when the generated cover is shown).
   */
  accessibilityLabel?: string;
  /** Hide the cover from assistive tech, e.g. when the row already announces the title. */
  decorative?: boolean;
  /** Placed on the image or generated cover itself (it has the exact width and height). */
  testID?: string;
  style?: StyleProp<ViewStyle>;
}

/**
 * A book cover in a fixed 2:3 frame. Draws `coverUrl`, or a typographic cover
 * made from the title when there is no URL or the image fails to load.
 * The image element is isolated in `CoverImage` so it can be swapped for
 * `expo-image` later (#901).
 */
function BookCoverBase({
  title,
  coverUrl,
  size,
  accessibilityLabel,
  decorative = false,
  testID,
  style,
}: BookCoverProps) {
  const { theme } = useTheme();
  const { t } = useTranslation('common');
  // Remember WHICH url failed, so a new url gets a fresh attempt.
  const [failedUrl, setFailedUrl] = useState<string | null>(null);

  const { width, height } = COVER_SIZES[size];
  const showImage = !!coverUrl && failedUrl !== coverUrl;
  const handleError = useCallback(() => setFailedUrl(coverUrl ?? null), [coverUrl]);
  const radius = size === 'md' || size === 'lg' ? tokens.radius.coverLarge : tokens.radius.cover;

  const label =
    accessibilityLabel ?? (showImage ? t('coverAlt', { title }) : t('noCoverAvailable'));
  const a11yProps = decorative
    ? ({
        accessible: false,
        importantForAccessibility: 'no-hide-descendants',
        accessibilityElementsHidden: true,
        // react-native-web ignores the two native props above; it needs aria-hidden.
        'aria-hidden': true,
      } as const)
    : ({ accessible: true, accessibilityRole: 'image', accessibilityLabel: label } as const);

  return (
    <View
      {...a11yProps}
      style={[
        styles.shadow,
        { width, height, borderRadius: radius },
        { backgroundColor: theme.colors.coverBlank, shadowColor: theme.colors.shadow },
        style,
      ]}
    >
      <View style={[styles.clip, { borderRadius: radius }]}>
        {showImage ? (
          <CoverImage
            uri={coverUrl}
            width={width}
            height={height}
            radius={radius}
            testID={testID}
            onError={handleError}
          />
        ) : (
          <FallbackCover
            title={title}
            size={size}
            width={width}
            height={height}
            radius={radius}
            testID={testID}
          />
        )}
        {/* Spine shade: a translucent strip derived from the shadow token. */}
        <View
          pointerEvents="none"
          style={[
            styles.spine,
            { width: Math.max(3, Math.round(width * 0.08)), backgroundColor: theme.colors.shadow },
          ]}
        />
      </View>
    </View>
  );
}

/** The one place that draws the cover bitmap; swap for `expo-image` in #901. */
const CoverImage = memo(function CoverImage({
  uri,
  width,
  height,
  radius,
  testID,
  onError,
}: {
  uri: string;
  width: number;
  height: number;
  radius: number;
  testID?: string;
  onError: () => void;
}) {
  return (
    <Image
      source={{ uri }}
      style={{ width, height, borderRadius: radius }}
      resizeMode="cover"
      onError={onError}
      accessible={false}
      testID={testID}
    />
  );
});

const FallbackCover = memo(function FallbackCover({
  title,
  size,
  width,
  height,
  radius,
  testID,
}: {
  title: string;
  size: BookCoverSize;
  width: number;
  height: number;
  radius: number;
  testID?: string;
}) {
  const { theme } = useTheme();
  const text = fallbackTitleStyle(title, size);
  const inset = fallbackInset(width);
  return (
    <View
      style={[
        styles.fallback,
        { width, height, borderRadius: radius, backgroundColor: theme.colors.coverBlank },
      ]}
      testID={testID}
    >
      <View
        pointerEvents="none"
        style={[
          styles.innerFrame,
          {
            top: inset,
            left: inset,
            right: inset,
            bottom: inset,
            borderColor: theme.colors.onCoverBlank,
          },
        ]}
      />
      {!text.placeholder && (
        <Text
          style={{
            fontFamily: fontFor('600', 'serif'),
            width: text.width,
            fontSize: text.fontSize,
            lineHeight: text.lineHeight,
            color: theme.colors.onCoverBlank,
            textAlign: 'center',
          }}
          numberOfLines={text.lines}
          // The frame already carries the label; hide the typeset title so it is not read twice.
          accessibilityElementsHidden
          aria-hidden
          importantForAccessibility="no-hide-descendants"
          ellipsizeMode="tail"
          allowFontScaling={false}
        >
          {text.text}
        </Text>
      )}
    </View>
  );
});

export const BookCover = memo(BookCoverBase);

const styles = StyleSheet.create({
  shadow: {
    shadowOffset: { width: 0, height: 1 },
    shadowOpacity: 0.18,
    shadowRadius: 3,
    elevation: 2,
  },
  clip: { flex: 1, overflow: 'hidden' },
  spine: { position: 'absolute', top: 0, bottom: 0, left: 0, opacity: 0.12 },
  fallback: { alignItems: 'center', justifyContent: 'center', overflow: 'hidden' },
  innerFrame: { position: 'absolute', borderWidth: StyleSheet.hairlineWidth, opacity: 0.45 },
});
