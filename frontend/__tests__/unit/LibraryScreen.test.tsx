import { render } from '@testing-library/react-native';
import { Text } from 'react-native';

import { LibraryScreen } from '../../components/library/LibraryScreen';

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({ theme: { colors: { background: '#fafafa' } } }),
}));

describe('LibraryScreen', () => {
  it('renders its children inside a full-height container on the theme background', async () => {
    const { getByText } = await render(
      <LibraryScreen>
        <Text>inside</Text>
      </LibraryScreen>
    );
    const parent = getByText('inside').parent!;
    const style = [parent.props.style].flat(Infinity).filter(Boolean);
    expect(style).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ flex: 1 }),
        expect.objectContaining({ backgroundColor: '#fafafa' }),
      ])
    );
  });
});
