/**
 * #910 tab bar: visible sentence-case labels (tabLabel 11/13, Inter 500, 600
 * when active), a height derived from the safe-area inset, and the active pill
 * (primarySoft, 56x30) behind the icon only. Same treatment in both themes.
 */
import React from 'react';
import { render } from '@testing-library/react-native';

import TabLayout from '../../app/(tabs)/_layout';
import { ThemeContext, darkTheme, lightTheme } from '../../theme';
import type { Theme } from '../../theme/light';

const g = global as any;

jest.mock('react-native-safe-area-context', () => ({
  useSafeAreaInsets: () => (global as any).__mockInsets,
}));

jest.mock('expo-router', () => {
  const React = require('react');
  const { View } = require('react-native');
  const g = global as any;
  function MockTabs({
    children,
    screenOptions,
  }: {
    children: React.ReactNode;
    screenOptions: any;
  }) {
    g.__opts = typeof screenOptions === 'function' ? screenOptions({ route: {} }) : screenOptions;
    return React.createElement(View, null, children);
  }
  function MockScreen(props: any) {
    g.__screens.push(props);
    return null;
  }
  MockTabs.Screen = MockScreen;
  return { Tabs: MockTabs };
});

jest.mock('@expo/vector-icons', () => {
  const React = require('react');
  const { Text } = require('react-native');
  return {
    MaterialIcons: ({ name, color }: { name: string; color: string }) =>
      React.createElement(Text, { testID: `icon-${name}`, style: { color } }, name),
  };
});

const inTheme = (theme: Theme, el: React.ReactElement) => (
  <ThemeContext.Provider
    value={{ theme, mode: theme.isDark ? 'dark' : 'light', toggleTheme: jest.fn() }}
  >
    {el}
  </ThemeContext.Provider>
);

const flat = (style: unknown) => Object.assign({}, ...[style].flat(Infinity).filter(Boolean));

async function renderIn(theme: Theme, bottomInset = 0) {
  g.__mockInsets = { top: 0, bottom: bottomInset, left: 0, right: 0 };
  g.__screens = [];
  g.__opts = {};
  await render(inTheme(theme, <TabLayout />));
  return g.__opts;
}

describe.each([
  ['light', lightTheme],
  ['dark', darkTheme],
])('tab bar (%s)', (_mode, theme) => {
  it('uses the single accent: primary active, onSurfaceVariant inactive, no tab background', async () => {
    const opts = await renderIn(theme);
    expect(opts.tabBarActiveTintColor).toBe(theme.colors.primary);
    expect(opts.tabBarInactiveTintColor).toBe(theme.colors.onSurfaceVariant);
    // The pill sits behind the icon only, never behind the whole tab.
    expect(opts.tabBarActiveBackgroundColor).toBe('transparent');
    expect(opts.tabBarInactiveBackgroundColor).toBe('transparent');
    // No gold accent in either mode (the old dark tertiary was #fbbc00).
    expect(String(opts.tabBarActiveTintColor).toLowerCase()).not.toBe('#fbbc00');
  });

  it('bar background is surfaceBar with a hairline outline top border', async () => {
    const opts = await renderIn(theme);
    const style = flat(opts.tabBarStyle);
    expect(style.backgroundColor).toBe(theme.colors.surfaceBar);
    expect(style.borderTopColor).toBe(theme.colors.outline);
    expect(style.borderTopWidth).toBeGreaterThan(0);
    expect(style.borderTopWidth).toBeLessThanOrEqual(1);
  });

  it('renders each label in sentence case with the tabLabel style', async () => {
    const opts = await renderIn(theme);
    const { getByText } = await render(
      opts.tabBarLabel({
        focused: false,
        color: theme.colors.onSurfaceVariant,
        children: 'My Books',
      })
    );
    const s = flat(getByText('My Books').props.style);
    expect(s.fontFamily).toBe('Inter_500Medium');
    expect(s.fontSize).toBe(11);
    expect(s.lineHeight).toBe(13);
    expect(s.textTransform).toBeUndefined();
    expect(s.fontWeight).toBeUndefined();
    expect(s.color).toBe(theme.colors.onSurfaceVariant);
    expect(getByText('My Books').props.maxFontSizeMultiplier).toBe(1.3);
  });

  it('the active label uses Inter 600', async () => {
    const opts = await renderIn(theme);
    const { getByText } = await render(
      opts.tabBarLabel({ focused: true, color: theme.colors.primary, children: 'Scan' })
    );
    const s = flat(getByText('Scan').props.style);
    expect(s.fontFamily).toBe('Inter_600SemiBold');
    expect(s.color).toBe(theme.colors.primary);
  });

  it('active icon sits in a 56x30 primarySoft pill; inactive has no pill', async () => {
    await renderIn(theme);
    const scan = g.__screens.find((s: any) => s.name === 'scan');
    const active = await render(
      inTheme(
        theme,
        scan.options.tabBarIcon({ focused: true, color: theme.colors.primary, size: 24 })
      )
    );
    const pill = flat(active.getByTestId('tab-pill-document-scanner').props.style);
    expect(pill.width).toBe(56);
    expect(pill.height).toBe(30);
    expect(pill.backgroundColor).toBe(theme.colors.primarySoft);
    expect(flat(active.getByTestId('icon-document-scanner').props.style).color).toBe(
      theme.colors.primary
    );

    const inactive = await render(
      inTheme(
        theme,
        scan.options.tabBarIcon({ focused: false, color: theme.colors.onSurfaceVariant, size: 24 })
      )
    );
    expect(inactive.queryByTestId('tab-pill-document-scanner')).toBeNull();
  });
});

describe('tab bar height (#910)', () => {
  it('grows with the bottom safe-area inset instead of a fixed 64/80', async () => {
    const none = flat((await renderIn(lightTheme, 0)).tabBarStyle);
    const notch = flat((await renderIn(lightTheme, 34)).tabBarStyle);
    expect(notch.height - none.height).toBe(34);
    expect(notch.paddingBottom - none.paddingBottom).toBe(34);
    expect([64, 80]).not.toContain(none.height);
  });

  it('leaves room for the pill, the gap and the label at the capped font scale', async () => {
    const style = flat((await renderIn(lightTheme, 0)).tabBarStyle);
    const content = style.height - style.paddingTop - style.paddingBottom;
    // pill 30 + gap 4 + label 13 * 1.3 rounded up (17)
    expect(content).toBeGreaterThanOrEqual(30 + 4 + 17);
    expect(content).toBeGreaterThanOrEqual(44);
  });

  it('keeps the accessibility label on every tab', async () => {
    await renderIn(lightTheme);
    const labels = g.__screens.map((s: any) => s.options.tabBarAccessibilityLabel);
    expect(labels).toHaveLength(4);
    labels.forEach((l: unknown) => expect(typeof l).toBe('string'));
    expect(new Set(labels).size).toBe(4);
  });
});
