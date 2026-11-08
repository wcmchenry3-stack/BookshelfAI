import React from 'react';
import { StyleSheet } from 'react-native';
import { fireEvent, render, screen } from '@testing-library/react-native';

import { ListRow, ListSection } from '../../components/ui';
import { lightTheme } from '../../theme';

const style = (el: { props: { style?: unknown } }) => StyleSheet.flatten(el.props.style as any);

const iconColour = (el: any) => (StyleSheet.flatten(el.props.style) as any).color;

// Starts a press without releasing it, so the Pressable's `pressed` style applies.
const pressIn = (el: any) =>
  fireEvent(el, 'responderGrant', {
    nativeEvent: { touches: [], changedTouches: [], pageX: 0, pageY: 0, timestamp: 0 },
    currentTarget: { measure: () => {}, measureInWindow: () => {} },
    persist: () => {},
  });

describe('ListRow', () => {
  it('renders label, hint and value; press handler fires', async () => {
    const onPress = jest.fn();
    await render(
      <ListRow label="Language" hint="App language" value="English" onPress={onPress} testID="r" />
    );
    expect(screen.getByText('Language')).toBeTruthy();
    expect(screen.getByText('App language')).toBeTruthy();
    expect(screen.getByText('English')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Language, English, App language' })).toBeTruthy();
    await fireEvent.press(screen.getByTestId('r'));
    expect(onPress).toHaveBeenCalledTimes(1);
  });

  it('is at least 52px tall', async () => {
    await render(<ListRow label="Language" onPress={jest.fn()} testID="r" />);
    expect(style(screen.getByTestId('r')).minHeight).toBeGreaterThanOrEqual(52);
  });

  it('shows a chevron when pressable, none when static, and honours the override', async () => {
    const { rerender } = await render(<ListRow label="A" onPress={jest.fn()} />);
    expect(screen.queryByTestId('list-row-chevron')).toBeTruthy();
    await rerender(<ListRow label="A" />);
    expect(screen.queryByTestId('list-row-chevron')).toBeNull();
    await rerender(<ListRow label="A" chevron />);
    expect(screen.queryByTestId('list-row-chevron')).toBeTruthy();
  });

  it('renders an icon tile in primarySoft', async () => {
    await render(<ListRow label="A" icon="language" onPress={jest.fn()} />);
    const icon = screen.getByTestId('list-row-icon');
    expect(iconColour(icon)).toBe(lightTheme.colors.primary);
  });

  it('destructive uses the danger colour for label and icon', async () => {
    await render(<ListRow label="Sign out" icon="logout" destructive onPress={jest.fn()} />);
    expect(style(screen.getByText('Sign out')).color).toBe(lightTheme.colors.danger);
    expect(iconColour(screen.getByTestId('list-row-icon'))).toBe(lightTheme.colors.danger);
  });

  it('static row is not a button', async () => {
    await render(<ListRow label="Version" value="1.0" testID="v" />);
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.getByTestId('v').props.accessibilityLabel).toBeUndefined();
    expect(screen.getByText('1.0')).toBeTruthy();
  });

  it('disabled row blocks presses and announces disabled', async () => {
    const onPress = jest.fn();
    await render(<ListRow label="A" onPress={onPress} disabled testID="r" />);
    await fireEvent.press(screen.getByTestId('r'));
    expect(onPress).not.toHaveBeenCalled();
    expect(screen.getByTestId('r').props.accessibilityState).toMatchObject({ disabled: true });
  });

  it('dims when pressed and when disabled', async () => {
    const { rerender } = await render(<ListRow label="A" onPress={jest.fn()} testID="r" />);
    expect(style(screen.getByTestId('r')).opacity).toBe(1);
    await pressIn(screen.getByTestId('r'));
    expect(style(screen.getByTestId('r')).opacity).toBe(0.7);
    await rerender(<ListRow label="A" onPress={jest.fn()} disabled testID="r" />);
    expect(style(screen.getByTestId('r')).opacity).toBe(0.4);
  });

  it('derives icon and chevron test IDs from the row testID', async () => {
    await render(<ListRow label="A" icon="language" onPress={jest.fn()} testID="r" />);
    expect(screen.getByTestId('r-icon')).toBeTruthy();
    expect(screen.getByTestId('r-chevron')).toBeTruthy();
  });
});

describe('ListSection', () => {
  it('renders an optional title and its rows', async () => {
    await render(
      <ListSection title="Account">
        <ListRow label="One" />
        <ListRow label="Two" />
      </ListSection>
    );
    expect(screen.getByRole('header', { name: 'Account' })).toBeTruthy();
    expect(screen.getByText('One')).toBeTruthy();
    expect(screen.getByText('Two')).toBeTruthy();
  });

  it('puts an inset divider between rows only', async () => {
    await render(
      <ListSection>
        <ListRow label="One" />
        <ListRow label="Two" />
        {false}
        <ListRow label="Three" />
      </ListSection>
    );
    const dividers = screen.getAllByTestId('list-divider');
    expect(dividers).toHaveLength(2);
    expect(style(dividers[0]).marginLeft).toBeGreaterThan(0);
  });

  it('has no title or divider for a single row', async () => {
    await render(
      <ListSection>
        <ListRow label="One" />
      </ListSection>
    );
    expect(screen.queryByRole('header')).toBeNull();
    expect(screen.queryByTestId('list-divider')).toBeNull();
  });

  it('is a surface card with the card radius', async () => {
    await render(
      <ListSection testID="sec">
        <ListRow label="One" />
      </ListSection>
    );
    const card = screen.getByTestId('sec').children[0] as any;
    const s = StyleSheet.flatten(card.props.style) as any;
    expect(s.backgroundColor).toBe(lightTheme.colors.surface);
    expect(s.borderRadius).toBe(lightTheme.radius.card);
  });

  it('insets the divider 16 when the row has no icon, 60 when it has one', async () => {
    await render(
      <ListSection>
        <ListRow label="One" icon="language" />
        <ListRow label="Two" />
        <ListRow label="Three" icon="language" />
      </ListSection>
    );
    const [d1, d2] = screen.getAllByTestId('list-divider');
    expect(style(d1).marginLeft).toBe(16);
    expect(style(d2).marginLeft).toBe(60);
  });
});

describe('ListRow selected (#933)', () => {
  it('shows a check instead of the chevron and reports selected', async () => {
    const { getByTestId, queryByTestId } = await render(
      <ListRow label="Title A–Z" onPress={jest.fn()} selected testID="r" />
    );
    expect(getByTestId('r-check')).toBeTruthy();
    expect(queryByTestId('r-chevron')).toBeNull();
    expect(getByTestId('r').props.accessibilityState).toEqual({ disabled: false, selected: true });
  });

  it('an unselected choice reports selected: false and keeps no check', async () => {
    const { queryByTestId, getByTestId } = await render(
      <ListRow label="Title A–Z" onPress={jest.fn()} selected={false} testID="r" />
    );
    expect(queryByTestId('r-check')).toBeNull();
    expect(getByTestId('r').props.accessibilityState).toEqual({ disabled: false, selected: false });
  });
});
