import React from 'react';
import { View } from 'react-native';
import { render } from '@testing-library/react-native';

import SettingsScreen from '../../app/(tabs)/settings';

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: { background: '#fff', text: '#000' },
      spacing: { md: 16 },
    },
  }),
}));

const MockThemeToggleButton = ({ testID }: { testID?: string }) => <View testID={testID} />;
jest.mock('../../components/ThemeToggleButton', () => ({
  ThemeToggleButton: () => <MockThemeToggleButton testID="theme-toggle-button" />,
}));

describe('SettingsScreen', () => {
  it('renders the settings heading', async () => {
    const { getByText } = await render(<SettingsScreen />);
    expect(getByText('Settings')).toBeTruthy();
  });

  it('renders the ThemeToggleButton', async () => {
    const { getByTestId } = await render(<SettingsScreen />);
    expect(getByTestId('theme-toggle-button')).toBeTruthy();
  });

  it('applies theme background color', async () => {
    const { toJSON } = await render(<SettingsScreen />);
    const tree = toJSON();
    // Root View should have the mocked background color
    expect(tree).toBeTruthy();
    if (!tree || Array.isArray(tree)) throw new Error('expected single root element');
    const rootStyle = Array.isArray(tree.props.style) ? tree.props.style : [tree.props.style];
    const hasBackground = rootStyle.some(
      (s: Record<string, unknown>) => s?.backgroundColor === '#fff'
    );
    expect(hasBackground).toBe(true);
  });
});
