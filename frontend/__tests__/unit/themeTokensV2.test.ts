/**
 * Design tokens v2 (#898): approved values, every role in both themes, the
 * single accent, spacing, radii, and WCAG AA contrast of the text pairs.
 */
import { darkTheme, lightTheme, palette, tokens } from '../../theme';

const APPROVED_LIGHT: Record<string, string> = {
  background: '#F6F4EF',
  surface: '#FFFFFF',
  surfaceBar: '#FBFAF7',
  field: '#ECE8DF',
  onSurface: '#1A1C20',
  onSurfaceVariant: '#5E636B',
  outline: '#E4DFD4',
  outlineCard: '#ECE7DC',
  outlineChip: '#DAD4C7',
  primary: '#0F426F',
  onPrimary: '#FFFFFF',
  primarySoft: '#DCE6F1',
  danger: '#B3261E',
  dangerSoft: '#F7E3DF',
  success: '#2F7A4F',
  statusWishlist: '#A56A12',
  statusPurchased: '#5F6F86',
  statusReading: '#0F426F',
  statusRead: '#2F7A4F',
  coverBlank: '#E7E1D3',
  onCoverBlank: '#4A4740',
};

const APPROVED_DARK: Record<string, string> = {
  background: '#121417',
  surface: '#1B1E23',
  surfaceBar: '#16181C',
  field: '#1F2227',
  onSurface: '#ECE9E3',
  onSurfaceVariant: '#A4A8AF',
  outline: '#2A2D33',
  outlineChip: '#33363C',
  primary: '#A9C9EE',
  onPrimary: '#0B2540',
  primarySoft: '#22344A',
  danger: '#FFB4AB',
  success: '#7FC59B',
  statusWishlist: '#E0B062',
  statusPurchased: '#A3B1C6',
  statusReading: '#A9C9EE',
  statusRead: '#7FC59B',
  coverBlank: '#2A2C30',
  onCoverBlank: '#C9C4BA',
};

const NEW_ROLES = [
  'surfaceBar',
  'field',
  'outlineCard',
  'outlineChip',
  'primarySoft',
  'danger',
  'dangerSoft',
  'success',
  'statusWishlist',
  'statusPurchased',
  'statusReading',
  'statusRead',
  'coverBlank',
  'onCoverBlank',
];

const LEGACY_ALIASES = [
  'background',
  'text',
  'textSecondary',
  'border',
  'iconActive',
  'iconInactive',
  'success',
  'tertiary',
  'onTertiary',
  'tertiaryFixed',
  'onTertiaryFixed',
  'secondary',
  'secondaryContainer',
  'onSecondaryContainer',
  'surfaceContainerLow',
  'surfaceContainerHigh',
  'surfaceContainerHighest',
  'error',
  'errorContainer',
  'onErrorContainer',
];

// WCAG 2.x relative luminance / contrast ratio.
function luminance(hex: string): number {
  const h = hex.replace('#', '');
  const [r, g, b] = [0, 2, 4].map((i) => {
    const v = parseInt(h.slice(i, i + 2), 16) / 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

describe('design tokens v2', () => {
  it('light values match the approved list exactly', () => {
    for (const [k, v] of Object.entries(APPROVED_LIGHT)) {
      expect([k, (lightTheme.colors as Record<string, string>)[k]]).toEqual([k, v]);
    }
  });

  it('dark values match the approved list exactly', () => {
    for (const [k, v] of Object.entries(APPROVED_DARK)) {
      expect([k, (darkTheme.colors as Record<string, string>)[k]]).toEqual([k, v]);
    }
  });

  it('every token is defined, as a non-empty string, in both themes', () => {
    const keys = Object.keys(tokens.colors);
    expect(Object.keys(lightTheme.colors).sort()).toEqual([...keys].sort());
    expect(Object.keys(darkTheme.colors).sort()).toEqual([...keys].sort());
    for (const k of keys) {
      expect(typeof (lightTheme.colors as Record<string, string>)[k]).toBe('string');
      expect((lightTheme.colors as Record<string, string>)[k]).not.toBe('');
      expect(typeof (darkTheme.colors as Record<string, string>)[k]).toBe('string');
      expect((darkTheme.colors as Record<string, string>)[k]).not.toBe('');
    }
    for (const k of [...NEW_ROLES, ...LEGACY_ALIASES]) {
      expect(keys).toContain(k);
    }
  });

  it('the palette module carries the same keys in light and dark', () => {
    expect(Object.keys(palette.dark).sort()).toEqual(Object.keys(palette.light).sort());
  });

  it('old aliases map onto the v2 palette', () => {
    for (const t of [lightTheme, darkTheme]) {
      const c = t.colors;
      expect(c.text).toBe(c.onSurface);
      expect(c.textSecondary).toBe(c.onSurfaceVariant);
      expect(c.border).toBe(c.outline);
      expect(c.iconActive).toBe(c.primary);
      expect(c.iconInactive).toBe(c.onSurfaceVariant);
      expect(c.error).toBe(c.danger);
    }
  });

  it('there is one accent: tertiary (the old dark gold) is the primary accent in both modes', () => {
    for (const t of [lightTheme, darkTheme]) {
      expect(t.colors.tertiary).toBe(t.colors.primary);
      expect(t.colors.onTertiary).toBe(t.colors.onPrimary);
      expect(t.colors.tertiaryFixed).toBe(t.colors.primarySoft);
      expect(t.colors.onTertiaryFixed).toBe(t.colors.primary);
    }
    expect(darkTheme.colors.tertiary.toLowerCase()).not.toBe('#fbbc00');
  });

  it('spacing scale is 4 / 8 / 12 / 16 / 24 / 32 / 48', () => {
    const values = Object.values(lightTheme.spacing).sort((a, b) => a - b);
    expect(values).toEqual([4, 8, 12, 16, 24, 32, 48]);
    expect(darkTheme.spacing).toBe(lightTheme.spacing);
  });

  it('radii: cover 3 (5 large), field 10, button 12, card 14, sheet 16, chip/pill full', () => {
    const r = lightTheme.radius;
    expect(r.cover).toBe(3);
    expect(r.coverLarge).toBe(5);
    expect(r.field).toBe(10);
    expect(r.button).toBe(12);
    expect(r.card).toBe(14);
    expect(r.sheet).toBe(16);
    expect(r.chip).toBe(r.full);
    expect(r.pill).toBe(r.full);
  });

  describe.each([
    ['light', lightTheme],
    ['dark', darkTheme],
  ])('%s contrast (WCAG AA 4.5:1)', (_m, t) => {
    const c = t.colors;
    it.each([
      ['onSurface', 'background'],
      ['onSurface', 'surface'],
      ['onSurfaceVariant', 'background'],
      ['onSurfaceVariant', 'surface'],
      ['primary', 'background'],
      ['primary', 'surface'],
      ['onPrimary', 'primary'],
      ['onCoverBlank', 'coverBlank'],
      ['danger', 'dangerSoft'],
      ['onSuccess', 'success'],
      ['primary', 'primarySoft'],
      ['onSurfaceVariant', 'surfaceBar'],
      ['primary', 'surfaceBar'],
    ])('%s on %s', (fg, bg) => {
      const ratio = contrast((c as Record<string, string>)[fg], (c as Record<string, string>)[bg]);
      expect(ratio).toBeGreaterThanOrEqual(4.5);
    });
  });
});
