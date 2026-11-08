import React from 'react';
import { fireEvent, render } from '@testing-library/react-native';

import { BookActionsSheet } from '../../components/library/BookActionsSheet';
import type { LibraryBook } from '../../components/library/libraryShared';

jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());
jest.mock('react-native-safe-area-context', () => ({
  useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }),
}));

const book = (status: string) =>
  ({
    id: 'ub-1',
    status,
    book: { id: 'b-1', title: 'Dune', author: 'Frank Herbert', cover_url: null },
    edition: null,
  }) as LibraryBook;

function setup(status: string, actionsDisabled = false) {
  const handlers = {
    onClose: jest.fn(),
    onAdvance: jest.fn(),
    onViewDetails: jest.fn(),
    onRemove: jest.fn(),
  };
  const view = render(
    <BookActionsSheet selected={book(status)} actionsDisabled={actionsDisabled} {...handlers} />
  );
  return { view, ...handlers };
}

describe('BookActionsSheet (#929)', () => {
  it('shows nothing without a selection', async () => {
    const { queryByText } = await render(
      <BookActionsSheet
        selected={null}
        actionsDisabled={false}
        onClose={() => {}}
        onAdvance={() => {}}
        onViewDetails={() => {}}
        onRemove={() => {}}
      />
    );
    expect(queryByText('View details')).toBeNull();
  });

  it.each([
    ['wishlisted', 'Mark as Purchased'],
    ['purchased', 'Mark as Reading'],
    ['reading', 'Mark as Read'],
  ])('offers "%s" -> advance, View details and Remove', async (status, advance) => {
    const { view } = setup(status);
    const { getByLabelText } = await view;
    expect(getByLabelText(advance)).toBeTruthy();
    expect(getByLabelText('View details')).toBeTruthy();
    expect(getByLabelText('Remove from library')).toBeTruthy();
  });

  it('has no advance row for a finished book', async () => {
    const { view } = setup('read');
    const { queryByLabelText, getByLabelText } = await view;
    expect(queryByLabelText(/Mark as/)).toBeNull();
    expect(getByLabelText('Remove from library')).toBeTruthy();
  });

  it('calls the matching handler with the book', async () => {
    const { view, onAdvance, onViewDetails, onRemove, onClose } = setup('wishlisted');
    const { getByLabelText } = await view;
    await fireEvent.press(getByLabelText('Mark as Purchased'));
    await fireEvent.press(getByLabelText('View details'));
    await fireEvent.press(getByLabelText('Remove from library'));
    await fireEvent.press(getByLabelText('Close'));
    expect(onAdvance).toHaveBeenCalledWith(expect.objectContaining({ id: 'ub-1' }));
    expect(onViewDetails).toHaveBeenCalledWith(expect.objectContaining({ id: 'ub-1' }));
    expect(onRemove).toHaveBeenCalledWith(expect.objectContaining({ id: 'ub-1' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('disables advance and remove (not View details) when offline or busy', async () => {
    const { view, onAdvance, onRemove } = setup('wishlisted', true);
    const { getByLabelText } = await view;
    expect(getByLabelText('Mark as Purchased').props.accessibilityState.disabled).toBe(true);
    expect(getByLabelText('Remove from library').props.accessibilityState.disabled).toBe(true);
    expect(getByLabelText('View details').props.accessibilityState.disabled).toBe(false);
    await fireEvent.press(getByLabelText('Mark as Purchased'));
    await fireEvent.press(getByLabelText('Remove from library'));
    expect(onAdvance).not.toHaveBeenCalled();
    expect(onRemove).not.toHaveBeenCalled();
  });
});
