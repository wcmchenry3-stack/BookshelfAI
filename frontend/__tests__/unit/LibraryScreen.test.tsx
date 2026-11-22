import { render } from '@testing-library/react-native';
import { Text } from 'react-native';

import { LibraryScreen } from '../../components/library/LibraryScreen';

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({ theme: { colors: { background: '#fafafa' } } }),
}));

jest.mock('react-native-safe-area-context', () => ({
  useSafeAreaInsets: () => ({ top: 47, bottom: 0, left: 0, right: 0 }),
}));

describe('LibraryScreen', () => {
  it('pads the top by the safe-area inset since the tabs have no header (#936)', async () => {
    const { getByText } = await render(
      <LibraryScreen>
        <Text>inside</Text>
      </LibraryScreen>
    );
    const style = [getByText('inside').parent!.props.style].flat(Infinity).filter(Boolean);
    expect(style).toEqual(expect.arrayContaining([expect.objectContaining({ paddingTop: 47 })]));
  });

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
