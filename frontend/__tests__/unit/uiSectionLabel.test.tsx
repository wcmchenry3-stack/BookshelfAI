import React from 'react';
import { StyleSheet } from 'react-native';
import { render, screen } from '@testing-library/react-native';

import { SectionLabel } from '../../components/ui';
import { lightTheme } from '../../theme';
import { CHROME_MAX_FONT_SCALE, textVariants } from '../../theme/typography';

describe('SectionLabel', () => {
  it('uses the sectionLabel text style in onSurfaceVariant', async () => {
    await render(<SectionLabel>Account</SectionLabel>);
    const el = screen.getByText('Account');
    const s = StyleSheet.flatten(el.props.style) as any;
    expect(s.fontFamily).toBe(textVariants.sectionLabel.fontFamily);
    expect(s.fontSize).toBe(13);
    expect(s.textTransform).toBe('uppercase');
    expect(s.color).toBe(lightTheme.colors.onSurfaceVariant);
    expect(s.fontWeight).toBeUndefined();
    expect(el.props.maxFontSizeMultiplier).toBe(CHROME_MAX_FONT_SCALE);
  });

  it('is announced as a header', async () => {
    await render(<SectionLabel>Account</SectionLabel>);
    expect(screen.getByRole('header', { name: 'Account' })).toBeTruthy();
  });
});
