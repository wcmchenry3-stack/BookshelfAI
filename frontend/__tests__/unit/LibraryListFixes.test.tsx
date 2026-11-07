import { StyleSheet } from 'react-native';
import { fireEvent, render } from '@testing-library/react-native';

import { BookRow, BookRowAction } from '../../components/library/BookRow';
import { StatusTabs, STATUS_TABS_HEIGHT } from '../../components/library/StatusTabs';
import type { LibraryBook } from '../../components/library/libraryShared';

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      isDark: false,
      colors: {
        background: '#fff',
        surface: '#f5f5f5',
        border: '#ccc',
        text: '#000',
        textSecondary: '#888',
        primary: '#0f426f',
        onPrimary: '#ffffff',
        tertiary: '#c9a227',
        onTertiary: '#000000',
        secondary: '#47645d',
        secondaryContainer: '#c6e7dd',
        onSecondaryContainer: '#4b6861',
        surfaceContainerLow: '#f5f3ef',
      },
      typography: { fontSizeBase: 16, fontSizeSM: 14, fontSizeXS: 12 },
    },
  }),
}));

function makeItem(title: string, cover: string | null): LibraryBook {
  return {
    id: 'ub-1',
    status: 'wishlisted',
    book: { id: 'b-1', title, author: 'An Author', cover_url: cover },
    edition: { publish_year: 1965 },
  } as unknown as LibraryBook;
}

describe('#905 StatusTabs layout', () => {
  it('is one fixed-height row that never takes flexible space', async () => {
    const { getByTestId } = await render(<StatusTabs activeTab="all" onChange={() => {}} />);
    const style = StyleSheet.flatten(getByTestId('status-tabs').props.style);
    expect(style.flexGrow).toBe(0);
    expect(style.flexShrink).toBe(0);
    expect(style.flex).toBeUndefined();
    expect(style.height).toBe(STATUS_TABS_HEIGHT);
  });
});

describe('#907 BookRow nesting', () => {
  it('keeps action buttons as siblings of the card button and does not open on action press', async () => {
    const onOpen = jest.fn();
    const onAction = jest.fn();
    const { getByTestId, getByLabelText } = await render(
      <BookRow
        item={makeItem('Dune', null)}
        categoryLabel="Wishlisted"
        onPress={onOpen}
        accessibilityLabel="Dune card"
      >
        <BookRowAction
          variant="primary"
          label="Mark"
          accessibilityLabel="mark action"
          onPress={onAction}
          disabled={false}
        />
      </BookRow>
    );
    const body = getByTestId('book-row-body');
    const action = getByLabelText('mark action');
    // The action is not a descendant of the card button.
    let node = action.parent;
    while (node) {
      expect(node).not.toBe(body);
      node = node.parent;
    }
    // The card button wraps nothing interactive.
    expect(body.children.length).toBe(0);

    await fireEvent.press(action);
    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onOpen).not.toHaveBeenCalled();

    await fireEvent.press(getByLabelText('Dune card'));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it('gives action buttons a 44px touch target', async () => {
    const { getByLabelText } = await render(
      <BookRowAction
        variant="primary"
        label="Mark"
        accessibilityLabel="mark action"
        onPress={() => {}}
        disabled={false}
      />
    );
    const props = getByLabelText('mark action').props;
    const style = StyleSheet.flatten(props.style);
    const slop = props.hitSlop;
    expect((style.minHeight ?? 0) + (slop?.top ?? 0) + (slop?.bottom ?? 0)).toBeGreaterThanOrEqual(
      44
    );
  });
});

describe('#911 BookRow cover frame', () => {
  it.each([
    ['short', 'Up'],
    ['long', 'A Very Long Title That Wraps Over Several Lines Of The Card And More And More'],
  ])('is a fixed 2:3 frame with a %s title, with and without a cover', async (_n, title) => {
    for (const cover of ['https://example.com/c.jpg', null]) {
      const { getByTestId, unmount } = await render(
        <BookRow item={makeItem(title, cover)} categoryLabel="Wishlisted">
          <BookRowAction
            variant="secondary"
            label="x"
            accessibilityLabel="x"
            onPress={() => {}}
            disabled={false}
          />
        </BookRow>
      );
      const style = StyleSheet.flatten(getByTestId('book-row-cover').props.style);
      expect(style.width! / style.height!).toBeCloseTo(2 / 3, 5);
      expect(style.alignSelf).not.toBe('stretch');
      await unmount();
    }
  });

  it('uses resizeMode cover', async () => {
    const { getByTestId } = await render(
      <BookRow item={makeItem('Dune', 'https://example.com/c.jpg')} categoryLabel="W">
        <></>
      </BookRow>
    );
    expect(getByTestId('book-row-cover').props.resizeMode).toBe('cover');
  });
});
