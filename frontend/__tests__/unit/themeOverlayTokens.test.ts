import { darkTheme, lightTheme } from '../../theme';

// #818 moved these literals into tokens; #898 redefines them for both modes on
// the v2 palette so no screen loses its colour.
describe('overlay and error-boundary tokens (v2 palette)', () => {
  it('dark mode header/tab bar/border/shadow', () => {
    expect(darkTheme.colors.headerOverlay).toBe('rgba(18,20,23,0.7)');
    expect(darkTheme.colors.tabBarOverlay).toBe('rgba(22,24,28,0.85)');
    expect(darkTheme.colors.outlineVariantFaint).toBe('rgba(42,45,51,0.4)');
    expect(darkTheme.colors.shadow).toBe('#000');
  });

  it('light mode header transparent, tab bar = surfaceBar, no border', () => {
    expect(lightTheme.colors.headerOverlay).toBe('transparent');
    expect(lightTheme.colors.tabBarOverlay).toBe(lightTheme.colors.surfaceBar);
    expect(lightTheme.colors.outlineVariantFaint).toBe('transparent');
    expect(lightTheme.colors.shadow).toBe('#000');
  });

  it.each([
    ['light', lightTheme],
    ['dark', darkTheme],
  ])('error boundary palette follows the %s v2 roles', (_m, theme) => {
    const c = theme.colors;
    expect(c.errorBoundarySurface).toBe(c.background);
    expect(c.errorBoundaryTitle).toBe(c.onSurface);
    expect(c.errorBoundaryBody).toBe(c.onSurfaceVariant);
    expect(c.errorBoundaryDetail).toBe(c.danger);
    expect(c.errorBoundaryDetailSurface).toBe(c.dangerSoft);
    expect(c.errorBoundaryAction).toBe(c.primary);
    expect(c.errorBoundaryOnAction).toBe(c.onPrimary);
  });
});
