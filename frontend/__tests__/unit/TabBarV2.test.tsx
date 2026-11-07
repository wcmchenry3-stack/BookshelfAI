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

  it('renders each label with the tabLabel style and no uppercase transform', async () => {
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

  // React Navigation renders each tab icon twice (focused and unfocused, cross-
  // faded by opacity), so in the app the pill element also exists, at opacity
  // 0, on inactive tabs. This checks the two renders our tabBarIcon returns.
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
  it('sizes to its content plus the bottom safe-area inset, never a fixed 64/80', async () => {
    const none = flat((await renderIn(lightTheme, 0)).tabBarStyle);
    const notch = flat((await renderIn(lightTheme, 34)).tabBarStyle);
    // No fixed height: the bar grows with its icon + label (and font scale).
    expect(none.height).toBe('auto');
    expect(notch.height).toBe('auto');
    // The floor and the bottom padding both carry the home-indicator inset.
    expect(notch.minHeight - none.minHeight).toBe(34);
    expect(notch.paddingBottom - none.paddingBottom).toBe(34);
    expect([64, 80]).not.toContain(none.minHeight);
  });

  // Regression for the clipped labels seen in the running app: React
  // Navigation's tab button adds its own padding around icon + label, which a
  // computed height must include. Read that padding from the installed
  // library so an upgrade that changes it fails here instead of clipping.
  it('the label fits inside the bar, including the tab button inner padding', async () => {
    const fs = require('fs');
    const src: string = fs.readFileSync(
      require.resolve('expo-router/build/react-navigation/bottom-tabs/views/BottomTabItem.js'),
      'utf8'
    );
    const m = src.match(/tabVerticalUiKit:\s*\{[^}]*?padding:\s*(\d+)/);
    expect(m).not.toBeNull();
    const itemPadding = Number(m![1]);

    const style = flat((await renderIn(lightTheme, 0)).tabBarStyle);
    const inner = style.minHeight - style.paddingTop - style.paddingBottom;
    // button padding + pill 30 + gap 4 + label line 13 at the 1.3 cap (17) + button padding
    const needed = itemPadding + 30 + 4 + Math.ceil(13 * 1.3) + itemPadding;
    expect(inner).toBeGreaterThanOrEqual(needed);
    expect(inner).toBeGreaterThanOrEqual(44);
    expect(style.paddingBottom).toBeGreaterThanOrEqual(4); // label never touches the edge
  });

  it('keeps the accessibility label on every tab', async () => {
    await renderIn(lightTheme);
    const labels = g.__screens.map((s: any) => s.options.tabBarAccessibilityLabel);
    expect(labels).toHaveLength(4);
    labels.forEach((l: unknown) => expect(typeof l).toBe('string'));
    expect(new Set(labels).size).toBe(4);
  });
});
