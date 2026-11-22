import React from 'react';
import { render } from '@testing-library/react-native';

import { AppText } from '../../components/ui/AppText';
import { ThemeContext, darkTheme, lightTheme } from '../../theme';
import {
  CHROME_MAX_FONT_SCALE,
  fontFor,
  textVariants,
  type TextVariant,
} from '../../theme/typography';

const flat = (style: unknown) => Object.assign({}, ...[style].flat(Infinity).filter(Boolean));

// #899 type specs: variant -> [font file, size, line height]
const SPEC: Record<TextVariant, [string, number, number]> = {
  screenTitle: ['NotoSerif_700Bold', 32, 40],
  sheetTitle: ['NotoSerif_700Bold', 24, 30],
  bookTitle: ['NotoSerif_600SemiBold', 16, 21],
  body: ['Inter_400Regular', 16, 21],
  bodyStrong: ['Inter_600SemiBold', 16, 21],
  secondary: ['Inter_400Regular', 14, 18],
  meta: ['Inter_400Regular', 13, 16],
  sectionLabel: ['Inter_600SemiBold', 13, 16],
  tabLabel: ['Inter_500Medium', 11, 13],
  tabLabelActive: ['Inter_600SemiBold', 11, 13],
};

describe('AppText', () => {
  it('covers every variant in the spec table', () => {
    expect(Object.keys(textVariants).sort()).toEqual(Object.keys(SPEC).sort());
  });

  it.each(Object.entries(SPEC))(
    '%s renders its font file, size and line height',
    async (v, spec) => {
      const [family, size, line] = spec;
      const { getByText } = await render(<AppText variant={v as TextVariant}>Hello</AppText>);
      const s = flat(getByText('Hello').props.style);
      expect(s.fontFamily).toBe(family);
      expect(s.fontSize).toBe(size);
      expect(s.lineHeight).toBe(line);
      // The weight comes from the font file, never fontWeight.
      expect(s.fontWeight).toBeUndefined();
    }
  );

  it('screenTitle tracks -0.4; sectionLabel is uppercase with +0.6 tracking', () => {
    expect(textVariants.screenTitle.letterSpacing).toBe(-0.4);
    expect(textVariants.sectionLabel.textTransform).toBe('uppercase');
    expect(textVariants.sectionLabel.letterSpacing).toBe(0.6);
    expect('textTransform' in textVariants.tabLabel).toBe(false);
  });

  it('defaults to body in onSurface from the theme', async () => {
    const { getByText } = await render(<AppText>Body</AppText>);
    const s = flat(getByText('Body').props.style);
    expect(s.fontFamily).toBe('Inter_400Regular');
    expect(s.color).toBe(lightTheme.colors.onSurface);
  });

  it('reads the colour role from the active theme', async () => {
    const { getByText } = await render(
      <ThemeContext.Provider value={{ theme: darkTheme, mode: 'dark', toggleTheme: jest.fn() }}>
        <AppText color="onSurfaceVariant">Dim</AppText>
      </ThemeContext.Provider>
    );
    expect(flat(getByText('Dim').props.style).color).toBe(darkTheme.colors.onSurfaceVariant);
  });

  it('caps font scaling for chrome text only', async () => {
    const { getByText } = await render(
      <>
        <AppText variant="tabLabel">Tab</AppText>
        <AppText variant="sectionLabel">Section</AppText>
        <AppText variant="body">Content</AppText>
      </>
    );
    expect(getByText('Tab').props.allowFontScaling).toBe(true);
    expect(getByText('Tab').props.maxFontSizeMultiplier).toBe(CHROME_MAX_FONT_SCALE);
    expect(getByText('Section').props.maxFontSizeMultiplier).toBe(CHROME_MAX_FONT_SCALE);
    expect(getByText('Content').props.allowFontScaling).toBe(true);
    expect(getByText('Content').props.maxFontSizeMultiplier).toBeUndefined();
  });

  it('applies a caller style last', async () => {
    const { getByText } = await render(
      <AppText variant="meta" style={{ marginTop: 4, color: 'red' }}>
        Styled
      </AppText>
    );
    const s = flat(getByText('Styled').props.style);
    expect(s.marginTop).toBe(4);
    expect(s.color).toBe('red');
    expect(s.fontFamily).toBe('Inter_400Regular');
  });
});

describe('fontFor', () => {
  it.each([
    ['400', 'sans', 'Inter_400Regular'],
    ['500', 'sans', 'Inter_500Medium'],
    ['600', 'sans', 'Inter_600SemiBold'],
    ['700', 'sans', 'Inter_700Bold'],
    ['600', 'serif', 'NotoSerif_600SemiBold'],
    ['700', 'serif', 'NotoSerif_700Bold'],
    ['400', 'serif', 'NotoSerif_600SemiBold'],
  ] as const)('%s %s -> %s', (w, f, file) => {
    expect(fontFor(w, f)).toBe(file);
  });

  it('defaults to Inter regular', () => {
    expect(fontFor()).toBe('Inter_400Regular');
  });
});
