// Typography (#899, #912).
//
// Custom fonts ship one file per weight, and Android ignores `fontWeight` on a
// custom family, so a weight is chosen by naming its font FILE. Never style
// text with `fontWeight` (alone or next to a custom `fontFamily`): use an
// `AppText` variant, or `fontFor(weight, family)` where AppText cannot be used.
//
// Font sizes and families do not change with the colour mode, so this module
// is static and can be imported directly (`../theme/typography`) by StyleSheets.

/** The font files loaded by `useFonts` in `app/_layout.tsx`. */
export const fontFiles = {
  sans: {
    '400': 'Inter_400Regular',
    '500': 'Inter_500Medium',
    '600': 'Inter_600SemiBold',
    '700': 'Inter_700Bold',
  },
  serif: {
    '600': 'NotoSerif_600SemiBold',
    '700': 'NotoSerif_700Bold',
  },
} as const;

export type FontWeight = '400' | '500' | '600' | '700';
export type FontFamilyKind = 'sans' | 'serif';

/**
 * The font file for a weight: Inter for `sans` (default), Noto Serif for
 * `serif`. Noto Serif is loaded at 600 and 700 only, so 400/500 map to 600.
 */
export function fontFor(weight: FontWeight = '400', family: FontFamilyKind = 'sans'): string {
  if (family === 'serif') {
    return weight === '700' ? fontFiles.serif['700'] : fontFiles.serif['600'];
  }
  return fontFiles.sans[weight];
}

/**
 * Largest Dynamic Type / font-scale multiplier for chrome text (tab labels,
 * section labels): they sit in fixed-height bars, so they grow up to 1.3x and
 * no further. Content text is not capped.
 */
export const CHROME_MAX_FONT_SCALE = 1.3;

export interface TextVariantStyle {
  fontFamily: string;
  fontSize: number;
  lineHeight: number;
  letterSpacing?: number;
  textTransform?: 'uppercase';
}

/** Named text styles. Each one names the font file for its weight. */
export const textVariants = {
  screenTitle: {
    fontFamily: fontFor('700', 'serif'),
    fontSize: 32,
    lineHeight: 40,
    letterSpacing: -0.4,
  },
  sheetTitle: { fontFamily: fontFor('700', 'serif'), fontSize: 24, lineHeight: 30 },
  bookTitle: { fontFamily: fontFor('600', 'serif'), fontSize: 16, lineHeight: 21 },
  body: { fontFamily: fontFor('400'), fontSize: 16, lineHeight: 21 },
  bodyStrong: { fontFamily: fontFor('600'), fontSize: 16, lineHeight: 21 },
  secondary: { fontFamily: fontFor('400'), fontSize: 14, lineHeight: 18 },
  meta: { fontFamily: fontFor('400'), fontSize: 13, lineHeight: 16 },
  sectionLabel: {
    fontFamily: fontFor('600'),
    fontSize: 13,
    lineHeight: 16,
    letterSpacing: 0.6,
    textTransform: 'uppercase',
  },
  tabLabel: { fontFamily: fontFor('500'), fontSize: 11, lineHeight: 13 },
  tabLabelActive: { fontFamily: fontFor('600'), fontSize: 11, lineHeight: 13 },
} as const satisfies Record<string, TextVariantStyle>;

export type TextVariant = keyof typeof textVariants;

/** Variants capped at CHROME_MAX_FONT_SCALE. */
export const chromeVariants: readonly TextVariant[] = [
  'sectionLabel',
  'tabLabel',
  'tabLabelActive',
];

export const typography = {
  // Font families (aliases kept while screens move to variants)
  fontFamilyHeadline: fontFiles.serif['700'],
  fontFamilyBody: fontFiles.sans['400'],

  variants: textVariants,

  // Scale
  fontSizeDisplay: 48,
  fontSizeH1: 40,
  fontSizeH2: 32,
  fontSizeH3: 24,
  fontSizeXL: 24, // kept for compat
  fontSizeLG: 20,
  fontSizeBase: 16,
  fontSizeSM: 14,
  fontSizeXS: 12,

  lineHeightBody: 1.5, // WCAG 1.4.12
  // No fontWeight* tokens: weights do not apply to custom fonts on Android.
  // Pick the font file with fontFor() or an AppText variant instead.
} as const;
