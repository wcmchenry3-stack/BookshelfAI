import React from 'react';
import { StyleSheet } from 'react-native';
import { fireEvent, render, screen } from '@testing-library/react-native';

import { Chip } from '../../components/ui';
import { lightTheme } from '../../theme';

const style = (el: { props: { style?: unknown } }) => StyleSheet.flatten(el.props.style as any);

// Starts a press without releasing it, so the Pressable's `pressed` style applies.
const pressIn = (el: any) =>
  fireEvent(el, 'responderGrant', {
    nativeEvent: { touches: [], changedTouches: [], pageX: 0, pageY: 0, timestamp: 0 },
    currentTarget: { measure: () => {}, measureInWindow: () => {} },
    persist: () => {},
  });

describe('Chip', () => {
  it('unselected: transparent with an outlineChip border, selected state false', async () => {
    await render(<Chip label="Read" onPress={jest.fn()} testID="c" />);
    const el = screen.getByTestId('c');
    const s = style(el);
    expect(s.backgroundColor).toBe('transparent');
    expect(s.borderColor).toBe(lightTheme.colors.outlineField);
    expect(el.props.accessibilityState).toMatchObject({ selected: false });
    expect(style(screen.getByText('Read')).color).toBe(lightTheme.colors.onSurface);
  });

  it('selected: primary fill, onPrimary text, selected state true', async () => {
    await render(<Chip label="Read" selected onPress={jest.fn()} testID="c" />);
    const el = screen.getByTestId('c');
    expect(style(el).backgroundColor).toBe(lightTheme.colors.primary);
    expect(el.props.accessibilityState).toMatchObject({ selected: true });
    expect(style(screen.getByText('Read')).color).toBe(lightTheme.colors.onPrimary);
  });

  it('shows the count and includes it in the accessible name', async () => {
    await render(<Chip label="Read" count={12} onPress={jest.fn()} />);
    expect(screen.getByText('Read 12')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Read 12' })).toBeTruthy();
  });

  it('has a 44px minimum height and a pill radius', async () => {
    await render(<Chip label="Read" onPress={jest.fn()} testID="c" />);
    const s = style(screen.getByTestId('c'));
    expect(s.minHeight).toBeGreaterThanOrEqual(44);
    expect(s.borderRadius).toBe(lightTheme.radius.chip);
  });

  it('calls onPress; disabled blocks it', async () => {
    const onPress = jest.fn();
    const { rerender } = await render(<Chip label="Read" onPress={onPress} testID="c" />);
    await fireEvent.press(screen.getByTestId('c'));
    expect(onPress).toHaveBeenCalledTimes(1);
    await rerender(<Chip label="Read" onPress={onPress} disabled testID="c" />);
    await fireEvent.press(screen.getByTestId('c'));
    expect(onPress).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('c').props.accessibilityState).toMatchObject({ disabled: true });
  });

  it('dims when pressed and when disabled', async () => {
    const { rerender } = await render(<Chip label="Read" onPress={jest.fn()} testID="c" />);
    expect(style(screen.getByTestId('c')).opacity).toBe(1);
    await pressIn(screen.getByTestId('c'));
    expect(style(screen.getByTestId('c')).opacity).toBe(0.85);
    await rerender(<Chip label="Read" onPress={jest.fn()} disabled testID="c" />);
    expect(style(screen.getByTestId('c')).opacity).toBe(0.4);
  });
});
