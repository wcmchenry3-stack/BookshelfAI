import React from 'react';
import type { StyleProp, ViewStyle } from 'react-native';
import Svg, { Path, Rect } from 'react-native-svg';

import { useTheme } from '../../hooks/useTheme';
import { LOGO_SHAPES, LOGO_VIEWBOX } from './logoPaths';

export interface BookshelfLogoProps {
  /** Width and height in points. Default 120. */
  size?: number;
  /** Corner radius of the field in points. Default 0 (square). */
  radius?: number;
  /** Spoken label. Callers pass translated copy. */
  accessibilityLabel: string;
  testID?: string;
  style?: StyleProp<ViewStyle>;
}

/**
 * The BookshelfAI mark (a chihuahua over an open book, in a ring, with the name on an arc), drawn
 * from the path data the asset recipe generates (`tools/assets/draw_logo.py`, `logoPaths.ts`).
 * Colours come from the theme: the field is `primary` and the ink is `onPrimary`, so it follows
 * light and dark mode. The native app icon and splash use the fixed brand navy instead.
 */
export const BookshelfLogo = React.memo(function BookshelfLogo({
  size = 120,
  radius = 0,
  accessibilityLabel,
  testID,
  style,
}: BookshelfLogoProps) {
  const { theme } = useTheme();
  const field = theme.colors.primary;
  const ink = theme.colors.onPrimary;
  const corner = radius * (LOGO_VIEWBOX / size);
  return (
    <Svg
      width={size}
      height={size}
      viewBox={`0 0 ${LOGO_VIEWBOX} ${LOGO_VIEWBOX}`}
      accessible
      accessibilityRole="image"
      accessibilityLabel={accessibilityLabel}
      testID={testID}
      style={style}
    >
      <Rect width={LOGO_VIEWBOX} height={LOGO_VIEWBOX} rx={corner} fill={field} />
      {LOGO_SHAPES.map((shape, i) => {
        const color = shape.ink === 'add' ? ink : field;
        return (
          <Path
            key={i}
            d={shape.d}
            fill={shape.fill ? color : 'none'}
            fillRule="evenodd"
            stroke={shape.stroke > 0 ? color : undefined}
            strokeWidth={shape.stroke > 0 ? shape.stroke : undefined}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        );
      })}
    </Svg>
  );
});
