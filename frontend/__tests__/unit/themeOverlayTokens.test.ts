import { darkTheme, lightTheme } from '../../theme';

// Regression (#818): values must equal the literals they replaced.
describe('overlay and error-boundary tokens keep their original values', () => {
  it('dark mode header/tab bar/border/shadow', () => {
    expect(darkTheme.colors.headerOverlay).toBe('rgba(17,19,23,0.7)');
    expect(darkTheme.colors.tabBarOverlay).toBe('rgba(17,19,23,0.85)');
    expect(darkTheme.colors.outlineVariantFaint).toBe('rgba(69,71,76,0.4)');
    expect(darkTheme.colors.shadow).toBe('#000');
  });

  it('light mode header transparent, tab bar = surfaceContainerLow, no border', () => {
    expect(lightTheme.colors.headerOverlay).toBe('transparent');
    expect(lightTheme.colors.tabBarOverlay).toBe(lightTheme.colors.surfaceContainerLow);
    expect(lightTheme.colors.outlineVariantFaint).toBe('transparent');
  });

  it('error boundary palette is identical in both modes', () => {
    const expected = {
      errorBoundarySurface: '#fff',
      errorBoundaryTitle: '#111',
      errorBoundaryBody: '#555',
      errorBoundaryDetail: '#c00',
      errorBoundaryDetailSurface: '#fff0f0',
      errorBoundaryAction: '#007AFF',
      errorBoundaryOnAction: '#fff',
    };
    for (const [k, v] of Object.entries(expected)) {
      expect((lightTheme.colors as Record<string, string>)[k]).toBe(v);
      expect((darkTheme.colors as Record<string, string>)[k]).toBe(v);
    }
  });
});
