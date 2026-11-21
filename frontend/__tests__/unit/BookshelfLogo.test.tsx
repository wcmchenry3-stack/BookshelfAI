import React from 'react';
import { processColor } from 'react-native';
import { render, screen } from '@testing-library/react-native';

import { BookshelfLogo } from '../../components/brand/BookshelfLogo';
import { LOGO_SHAPES, LOGO_VIEWBOX } from '../../components/brand/logoPaths';
import { darkTheme, lightTheme, ThemeContext } from '../../theme';

type Node = { type: unknown; props: Record<string, unknown>; children: unknown[] };

/** Every Path the component drew, in order (react-native-svg renders each as an RNSVGPath). */
function paths(): Node[] {
  const found: Node[] = [];
  const walk = (node: unknown) => {
    if (!node || typeof node !== 'object') return;
    const n = node as Node;
    if (n.type === 'RNSVGPath') found.push(n);
    n.children?.forEach(walk);
  };
  walk(screen.container);
  return found;
}

describe('BookshelfLogo', () => {
  it('is an image with the spoken label', async () => {
    await render(<BookshelfLogo accessibilityLabel="BookshelfAI" testID="logo" />);
    const logo = screen.getByRole('image', { name: 'BookshelfAI' });
    expect(logo).toBeTruthy();
    expect(screen.getByTestId('logo')).toBeTruthy();
  });

  it('draws every shape of the generated path data', async () => {
    await render(<BookshelfLogo accessibilityLabel="x" />);
    expect(paths()).toHaveLength(LOGO_SHAPES.length);
    expect(LOGO_VIEWBOX).toBe(1024);
  });

  // react-native-svg turns a colour string into `{ type, payload }` with the number processColor gives.
  const strokeColor = (node: Node) => (node.props.stroke as { payload: number }).payload;
  const firstStroked = (ink: 'add' | 'erase') =>
    LOGO_SHAPES.findIndex((s) => s.ink === ink && s.stroke > 0);

  it('paints add shapes in onPrimary and erase shapes in primary (light theme)', async () => {
    await render(<BookshelfLogo accessibilityLabel="x" />);
    const drawn = paths();
    expect(strokeColor(drawn[firstStroked('add')]!)).toBe(
      processColor(lightTheme.colors.onPrimary)
    );
    expect(strokeColor(drawn[firstStroked('erase')]!)).toBe(
      processColor(lightTheme.colors.primary)
    );
  });

  it('follows the theme: dark mode uses the dark primary and onPrimary', async () => {
    await render(
      <ThemeContext.Provider value={{ theme: darkTheme, mode: 'dark', toggleTheme: () => {} }}>
        <BookshelfLogo accessibilityLabel="x" />
      </ThemeContext.Provider>
    );
    const drawn = paths();
    expect(strokeColor(drawn[firstStroked('add')]!)).toBe(processColor(darkTheme.colors.onPrimary));
    expect(strokeColor(drawn[firstStroked('erase')]!)).toBe(processColor(darkTheme.colors.primary));
    expect(darkTheme.colors.onPrimary).not.toBe(lightTheme.colors.onPrimary);
  });

  it('keeps the generated path data free of colour literals', () => {
    for (const shape of LOGO_SHAPES) expect(shape.d).not.toMatch(/#[0-9a-f]{3,8}/i);
  });
});
