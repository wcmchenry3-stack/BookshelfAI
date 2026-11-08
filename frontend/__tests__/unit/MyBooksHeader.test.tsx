import { Text } from 'react-native';
import { render } from '@testing-library/react-native';

import { MyBooksHeader } from '../../components/library/MyBooksHeader';

jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());

describe('MyBooksHeader', () => {
  it('shows the title as a header and "{{count}} books · {{reading}} reading now"', async () => {
    const { getByRole, getByText } = await render(<MyBooksHeader total={10} reading={2} />);
    expect(getByRole('header', { name: 'My Books' })).toBeTruthy();
    expect(getByText('10 books · 2 reading now')).toBeTruthy();
  });

  it('leaves the reading part out when it is 0, and says "1 book"', async () => {
    const { getByText, rerender } = await render(<MyBooksHeader total={1} reading={0} />);
    expect(getByText('1 book')).toBeTruthy();
    await rerender(<MyBooksHeader total={0} reading={0} />);
    expect(getByText('0 books')).toBeTruthy();
  });

  it('renders the trailing slot', async () => {
    const { getByText } = await render(
      <MyBooksHeader total={3} reading={1} trailing={<Text>slot</Text>} />
    );
    expect(getByText('slot')).toBeTruthy();
  });
});
