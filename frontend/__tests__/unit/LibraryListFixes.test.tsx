import { StyleSheet } from 'react-native';
import { render } from '@testing-library/react-native';

import { StatusTabs, STATUS_TABS_HEIGHT } from '../../components/library/StatusTabs';

jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());

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
