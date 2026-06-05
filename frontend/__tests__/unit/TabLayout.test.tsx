import React from 'react';
import { render } from '@testing-library/react-native';

import TabLayout from '../../app/(tabs)/_layout';

// ─── Mocks ───────────────────────────────────────────────────────────────────

jest.mock('../../hooks/useTheme', () => ({
  useTheme: () => ({
    theme: {
      colors: {
        primary: '#0f426f',
        onPrimary: '#ffffff',
        secondary: '#47645d',
        surfaceContainerLow: '#f5f3ef',
        iconActive: '#0f426f',
        iconInactive: '#47645d',
        surface: '#fbf9f5',
        text: '#1b1c1a',
      },
    },
  }),
}));

const g = global as any;

jest.mock('expo-router', () => {
  const React = require('react');
  const { View, Text } = require('react-native');
  const g = global as any;

  function MockTabs({
    children,
    screenOptions,
  }: {
    children: React.ReactNode;
    screenOptions: any;
  }) {
    // screenOptions is a function in the new layout — call it to get the resolved object
    const resolved =
      typeof screenOptions === 'function'
        ? screenOptions({ route: { name: 'scan' } })
        : screenOptions;
    // Store on global so test assertions can access it (factories cannot close over outer vars)
    g.__mockTabScreenOptions = resolved;
    return React.createElement(View, { testID: 'tabs-container' }, children);
  }

  function MockScreen(props: any) {
    g.__mockScreenProps = (g.__mockScreenProps || []).concat([
      { name: props.name, options: props.options },
    ]);
    const icon =
      typeof props.options?.tabBarIcon === 'function'
        ? props.options.tabBarIcon({ focused: false, color: '#000', size: 24 })
        : null;
    return React.createElement(
      View,
      { testID: `tab-${props.name}` },
      React.createElement(Text, null, props.options?.title),
      icon
    );
  }

  MockTabs.Screen = MockScreen;
  return { Tabs: MockTabs };
});

jest.mock('@expo/vector-icons', () => {
  const React = require('react');
  const { Text } = require('react-native');
  const MockIcon = ({ name }: { name: string }) =>
    React.createElement(Text, { testID: `icon-${name}` }, name);
  return {
    Ionicons: MockIcon,
    MaterialIcons: MockIcon,
  };
});

beforeEach(() => {
  jest.clearAllMocks();
  g.__mockScreenProps = [];
  g.__mockTabScreenOptions = {};
});

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('TabLayout', () => {
  it('renders 4 tab screens', () => {
    render(<TabLayout />);
    expect(g.__mockScreenProps).toHaveLength(4);
  });

  it('includes scan, wishlist, my-books, and settings tabs', () => {
    render(<TabLayout />);
    const names = g.__mockScreenProps.map((s: { name: string }) => s.name);
    expect(names).toEqual(['scan', 'wishlist', 'my-books', 'settings']);
  });

  it('tab titles use i18n keys', () => {
    render(<TabLayout />);
    const titles = g.__mockScreenProps.map((s: { options: { title: string } }) => s.options.title);
    expect(titles).toEqual(['Scan', 'Wishlist', 'My Books', 'Settings']);
  });

  it('applies brand theme colors to tab bar via screenOptions', () => {
    render(<TabLayout />);
    const opts = g.__mockTabScreenOptions;
    expect(opts.tabBarActiveTintColor).toBe('#ffffff');
    expect(opts.tabBarInactiveTintColor).toBe('#47645d');
    expect(opts.tabBarStyle?.backgroundColor).toBe('#f5f3ef');
  });
});
