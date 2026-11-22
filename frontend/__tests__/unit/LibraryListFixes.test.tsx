import { StyleSheet } from 'react-native';
import { render } from '@testing-library/react-native';

import { StatusChips } from '../../components/library/StatusChips';

jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());

const COUNTS = { all: 10, wishlisted: 3, purchased: 2, reading: 2, read: 3 };

describe('#905 StatusChips layout', () => {
  it('is one row that never takes flexible space', async () => {
    const { getByTestId } = await render(
      <StatusChips activeTab="all" counts={COUNTS} onChange={() => {}} />
    );
    const style = StyleSheet.flatten(getByTestId('status-tabs').props.style);
    expect(style.flexGrow).toBe(0);
    expect(style.flexShrink).toBe(0);
    expect(style.flex).toBeUndefined();
    // No fixed height: the 44px chips grow with large text instead of clipping.
    expect(style.height).toBeUndefined();
  });
});
