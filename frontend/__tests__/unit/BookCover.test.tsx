import { Image, StyleSheet } from 'react-native';
import { fireEvent, render } from '@testing-library/react-native';

import { BookCover, COVER_SIZES, type BookCoverSize } from '../../components/ui/BookCover';
import { tokens } from '../../theme/tokens';
import { fontFor } from '../../theme/typography';

// fontFor() runs in the render body of the fallback cover, so its call count
// shows how often a fallback re-renders.
jest.mock('../../theme/typography', () => {
  const actual = jest.requireActual('../../theme/typography');
  return { ...actual, fontFor: jest.fn(actual.fontFor) };
});

// useTheme() runs in the render body of BookCover and of the fallback, so its
// call count shows how often they render.
const mockUseTheme = jest.fn(() => ({
  theme: { colors: { coverBlank: '#E7E1D3', onCoverBlank: '#4A4740', shadow: '#000' } },
}));
jest.mock('../../hooks/useTheme', () => ({ useTheme: () => mockUseTheme() }));

// Image renders as a View with the same props, and every render is recorded
// (calls[i][0] is the props), so CoverImage's renders and onError are visible.
jest.mock('react-native', () => {
  const rn = jest.requireActual('react-native');
  const ReactLib = require('react');
  const Image = jest.fn((props: Record<string, unknown>) => ReactLib.createElement(rn.View, props));
  return Object.create(rn, { Image: { value: Image } });
});

const SIZES: BookCoverSize[] = ['xs', 'sm', 'md', 'lg'];
const H = { includeHiddenElements: true };
const URL = 'https://example.com/c.jpg';

beforeEach(() => {
  jest.clearAllMocks();
});

describe('#902 BookCover', () => {
  it.each(SIZES)('is a fixed 2:3 frame at %s', async (size) => {
    const { getByTestId } = await render(
      <BookCover title="Dune" coverUrl={URL} size={size} testID="c" />
    );
    const style = StyleSheet.flatten(getByTestId('c').props.style);
    expect(style.width).toBe(COVER_SIZES[size].width);
    expect(style.height).toBe(COVER_SIZES[size].height);
    expect(style.width / style.height).toBeCloseTo(2 / 3, 5);
  });

  it('has the specified sizes', () => {
    expect(COVER_SIZES).toEqual({
      xs: { width: 48, height: 72 },
      sm: { width: 52, height: 78 },
      md: { width: 104, height: 156 },
      lg: { width: 136, height: 204 },
    });
  });

  it('draws the image with resizeMode cover when there is a url', async () => {
    const { getByTestId, queryByText } = await render(
      <BookCover title="Dune" coverUrl={URL} size="sm" testID="c" />
    );
    expect(getByTestId('c').props.resizeMode).toBe('cover');
    expect(queryByText('Dune', H)).toBeNull();
  });

  it('draws the typographic fallback with the title when cover_url is null', async () => {
    const { getByText, getByTestId } = await render(
      <BookCover title="Dune" coverUrl={null} size="md" testID="c" />
    );
    expect(getByText('Dune', H)).toBeTruthy();
    expect(getByTestId('c').props.resizeMode).toBeUndefined();
  });

  it('switches to the fallback after the image fails to load', async () => {
    const { getByTestId, getByText, queryByText } = await render(
      <BookCover title="Dune" coverUrl={URL} size="sm" testID="c" />
    );
    expect(queryByText('Dune', H)).toBeNull();
    await fireEvent(getByTestId('c'), 'error');
    expect(getByText('Dune', H)).toBeTruthy();
  });

  it('tries a new url again after an earlier one failed', async () => {
    const { getByTestId, getByText, rerender, queryByText } = await render(
      <BookCover title="Dune" coverUrl={URL} size="sm" testID="c" />
    );
    await fireEvent(getByTestId('c'), 'error');
    expect(getByText('Dune', H)).toBeTruthy();
    await rerender(
      <BookCover title="Dune" coverUrl="https://example.com/d.jpg" size="sm" testID="c" />
    );
    expect(queryByText('Dune', H)).toBeNull();
  });

  it('clamps a long title and uses the serif font file, not fontWeight', async () => {
    const { getByText } = await render(
      <BookCover title={'Very Long Title '.repeat(20)} coverUrl={null} size="xs" />
    );
    const node = getByText(/Very Long Title/, H);
    expect(node.props.numberOfLines).toBeGreaterThan(0);
    const style = StyleSheet.flatten(node.props.style);
    expect(style.fontFamily).toBe('NotoSerif_600SemiBold');
    expect(style.fontWeight).toBeUndefined();
    expect(style.color).toBe('#4A4740');
  });

  it.each([
    ['xs', tokens.radius.cover],
    ['sm', tokens.radius.cover],
    ['md', tokens.radius.coverLarge],
    ['lg', tokens.radius.coverLarge],
  ] as const)('uses the cover radius at %s', async (size, radius) => {
    const { getByTestId } = await render(
      <BookCover title="Dune" coverUrl={URL} size={size} testID="c" />
    );
    expect(StyleSheet.flatten(getByTestId('c').props.style).borderRadius).toBe(radius);
  });

  describe('accessibility', () => {
    it('labels the image from the title', async () => {
      const { getByLabelText } = await render(<BookCover title="Dune" coverUrl={URL} size="sm" />);
      expect(getByLabelText('Cover of Dune').props.accessibilityRole).toBe('image');
    });

    it('labels the fallback as an image (no role-less aria-label)', async () => {
      const { getByLabelText } = await render(<BookCover title="Dune" coverUrl={null} size="sm" />);
      expect(getByLabelText('No cover available').props.accessibilityRole).toBe('image');
    });

    it('uses the label the caller passes', async () => {
      const { getByLabelText } = await render(
        <BookCover title="Dune" coverUrl={URL} size="sm" accessibilityLabel="Custom" />
      );
      expect(getByLabelText('Custom')).toBeTruthy();
    });

    it('is hidden from assistive tech when decorative', async () => {
      const { queryByLabelText, toJSON } = await render(
        <BookCover title="Dune" coverUrl={URL} size="sm" decorative />
      );
      expect(queryByLabelText('Cover of Dune')).toBeNull();
      const root = toJSON() as { props: Record<string, unknown> };
      expect(root.props.accessible).toBe(false);
      expect(root.props.importantForAccessibility).toBe('no-hide-descendants');
    });
  });

  describe('web accessibility', () => {
    it('sets aria-hidden on the decorative frame and on the fallback title', async () => {
      const { toJSON, getByText } = await render(
        <BookCover title="Dune" coverUrl={null} size="sm" decorative />
      );
      expect((toJSON() as { props: Record<string, unknown> }).props['aria-hidden']).toBe(true);
      expect(getByText('Dune', H).props['aria-hidden']).toBe(true);
    });

    it('does not set aria-hidden on a labelled cover frame, but still hides the typeset title', async () => {
      const { toJSON, getByText } = await render(
        <BookCover title="Dune" coverUrl={null} size="sm" />
      );
      expect((toJSON() as { props: Record<string, unknown> }).props['aria-hidden']).toBeUndefined();
      expect(getByText('Dune', H).props['aria-hidden']).toBe(true);
    });
  });

  describe('performance (memoised)', () => {
    const books = Array.from({ length: 50 }, (_, i) => ({
      title: `Book ${i}`,
      url: i % 2 ? URL : null,
    }));
    const list = (label?: string) => (
      <>
        {books.map((b) => (
          <BookCover
            key={b.title}
            title={b.title}
            coverUrl={b.url}
            size="sm"
            accessibilityLabel={label}
          />
        ))}
      </>
    );

    const calls = (fn: unknown) => (fn as jest.Mock).mock.calls;
    const imageCalls = () => calls(Image);

    it('does not re-render 50 covers when re-rendered with unchanged props', async () => {
      const { rerender } = await render(list());
      const themeBefore = calls(mockUseTheme).length;
      const fontBefore = calls(fontFor).length;
      const imageBefore = imageCalls().length;
      expect(fontBefore).toBeGreaterThanOrEqual(25);
      expect(imageBefore).toBeGreaterThanOrEqual(25);
      await rerender(list());
      // BookCover itself (memo), its fallback and its image all skip the render.
      expect(calls(mockUseTheme).length).toBe(themeBefore);
      expect(calls(fontFor).length).toBe(fontBefore);
      expect(imageCalls().length).toBe(imageBefore);
    });

    it('re-renders only the frame, not the fallback or the image, when the label changes', async () => {
      const { rerender } = await render(list());
      const themeBefore = calls(mockUseTheme).length;
      const fontBefore = calls(fontFor).length;
      const imageBefore = imageCalls().length;
      await rerender(list('Changed'));
      // Non-vacuous: every BookCover frame did render again (one useTheme call each)...
      expect(calls(mockUseTheme).length - themeBefore).toBe(50);
      // ...but the memoised fallback and image did not (this needs a stable onError).
      expect(calls(fontFor).length).toBe(fontBefore);
      expect(imageCalls().length).toBe(imageBefore);
    });

    it('passes the same onError to the image across renders', async () => {
      const { rerender } = await render(
        <BookCover title="Dune" coverUrl={URL} size="sm" testID="c" />
      );
      const first = imageCalls()[0][0].onError;
      expect(typeof first).toBe('function');
      await rerender(
        <BookCover title="Dune" coverUrl={URL} size="sm" testID="c" accessibilityLabel="Other" />
      );
      for (const [props] of imageCalls()) expect(props.onError).toBe(first);
    });
  });
});
