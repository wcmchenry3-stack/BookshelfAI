import React from 'react';
import { Text, type TextProps } from 'react-native';

import { useTheme } from '../../hooks/useTheme';
import type { ColorKey } from '../../theme/light';
import {
  CHROME_MAX_FONT_SCALE,
  chromeVariants,
  textVariants,
  type TextVariant,
} from '../../theme/typography';

export interface AppTextProps extends TextProps {
  /** Named text style (#899). Defaults to `body`. */
  variant?: TextVariant;
  /** Theme colour role for the text. Defaults to `onSurface`. */
  color?: ColorKey;
}

/**
 * Themed text. The variant picks the font FILE for its weight (never
 * `fontWeight`), the size and the line height; the colour comes from
 * `useTheme()`. Chrome variants (tab and section labels) cap font scaling at
 * CHROME_MAX_FONT_SCALE; content variants scale freely. A `style` prop is
 * applied last, so callers can still set layout or override the colour.
 */
export function AppText({
  variant = 'body',
  color = 'onSurface',
  style,
  maxFontSizeMultiplier,
  ...rest
}: AppTextProps) {
  const { theme } = useTheme();
  const isChrome = chromeVariants.includes(variant);
  return (
    <Text
      allowFontScaling
      maxFontSizeMultiplier={
        maxFontSizeMultiplier ?? (isChrome ? CHROME_MAX_FONT_SCALE : undefined)
      }
      style={[textVariants[variant], { color: theme.colors?.[color] }, style]}
      {...rest}
    />
  );
}
