import { ScrollView } from 'react-native';
import { fireEvent, render } from '@testing-library/react-native';

import { StatusChips } from '../../components/library/StatusChips';
import { STATUS_TAB_KEYS } from '../../components/library/libraryShared';

jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());

const COUNTS = { all: 10, wishlisted: 3, purchased: 2, reading: 2, read: 1 };

describe('StatusChips', () => {
  it('renders every chip with its count, marks the active one and reports changes', async () => {
    const onChange = jest.fn();
    const { getByLabelText, getAllByRole } = await render(
      <StatusChips activeTab="reading" counts={COUNTS} onChange={onChange} />
    );
    expect(getAllByRole('button')).toHaveLength(STATUS_TAB_KEYS.length);
    expect(getByLabelText('Filter by Read, 1 book')).toBeTruthy();
    await fireEvent.press(getByLabelText('Filter by Purchased, 2 books'));
    expect(onChange).toHaveBeenCalledWith('purchased');
    const selected = getAllByRole('button').filter((b) => b.props.accessibilityState?.selected);
    expect(selected).toHaveLength(1);
    expect(selected[0].props.accessibilityLabel).toBe('Filter by Reading, 2 books');
  });

  it('pads both ends of the content so the last chip can scroll fully into view', async () => {
    const { getByTestId } = await render(
      <StatusChips activeTab="all" counts={COUNTS} onChange={() => {}} />
    );
    const content = getByTestId('status-tabs').props.contentContainerStyle;
    expect(content.paddingHorizontal).toBe(16);
  });

  describe('scrolls the selected chip into view', () => {
    let scrollTo: jest.SpyInstance;
    beforeEach(() => {
      scrollTo = jest.spyOn(ScrollView.prototype, 'scrollTo').mockImplementation(() => {});
    });
    afterEach(() => scrollTo.mockRestore());

    const layout = (x: number) => ({ nativeEvent: { layout: { x, y: 0, width: 90, height: 44 } } });

    it('on mount, once the chip is measured', async () => {
      const { getByTestId } = await render(
        <StatusChips activeTab="read" counts={COUNTS} onChange={() => {}} />
      );
      await fireEvent(getByTestId('status-chip-slot-read'), 'layout', layout(400));
      expect(scrollTo).toHaveBeenLastCalledWith({ x: 384, animated: false });
    });

    it('when the selection changes', async () => {
      const { getByTestId, rerender } = await render(
        <StatusChips activeTab="all" counts={COUNTS} onChange={() => {}} />
      );
      await fireEvent(getByTestId('status-chip-slot-all'), 'layout', layout(16));
      await fireEvent(getByTestId('status-chip-slot-reading'), 'layout', layout(300));
      await rerender(<StatusChips activeTab="reading" counts={COUNTS} onChange={() => {}} />);
      expect(scrollTo).toHaveBeenLastCalledWith({ x: 284, animated: true });
    });
  });
});
