import React from 'react';
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

const MockThemeToggleButton = () => <></>;
jest.mock('../../components/ThemeToggleButton', () => ({
  ThemeToggleButton: () => <MockThemeToggleButton />,
}));

describe('SettingsScreen', () => {
  it('renders the settings heading', () => {
    const { getByText } = render(<SettingsScreen />);
    expect(getByText('Settings')).toBeTruthy();
  });

  it('renders the ThemeToggleButton', () => {
    const { UNSAFE_getByType } = render(<SettingsScreen />);
    expect(UNSAFE_getByType(MockThemeToggleButton)).toBeTruthy();
  });

  it('applies theme background color', () => {
    const { toJSON } = render(<SettingsScreen />);
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
