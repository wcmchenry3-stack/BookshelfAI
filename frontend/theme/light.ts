import { tokens } from './tokens';

export type ColorKey = keyof typeof tokens.colors;

/** Every colour role resolved for one mode. */
export function colorsFor(mode: 'light' | 'dark'): Record<ColorKey, string> {
  const out = {} as Record<ColorKey, string>;
  for (const key of Object.keys(tokens.colors) as ColorKey[]) {
    out[key] = tokens.colors[key][mode];
  }
  return out;
}

export const lightTheme = {
  colors: colorsFor('light'),
  typography: tokens.typography,
  spacing: tokens.spacing,
  radius: tokens.radius,
  touchTarget: tokens.touchTarget,
  isDark: false,
} as const;

// Theme type: colors are string (not literal) so darkTheme can use different hex values.
export type Theme = {
  colors: Record<ColorKey, string>;
  typography: typeof tokens.typography;
  spacing: typeof tokens.spacing;
  radius: typeof tokens.radius;
  touchTarget: typeof tokens.touchTarget;
  isDark: boolean;
};
