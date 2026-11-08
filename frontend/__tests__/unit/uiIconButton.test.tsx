import React from 'react';
import { StyleSheet } from 'react-native';
import { fireEvent, render, screen } from '@testing-library/react-native';

import { IconButton } from '../../components/ui';

// Starts a press without releasing it, so the Pressable's `pressed` style applies.
const pressIn = (el: any) =>
  fireEvent(el, 'responderGrant', {
    nativeEvent: { touches: [], changedTouches: [], pageX: 0, pageY: 0, timestamp: 0 },
    currentTarget: { measure: () => {}, measureInWindow: () => {} },
    persist: () => {},
  });

describe('IconButton', () => {
  it('has a button role and the required accessibility label', async () => {
    await render(<IconButton icon="close" accessibilityLabel="Close" onPress={jest.fn()} />);
    expect(screen.getByRole('button', { name: 'Close' })).toBeTruthy();
  });

  it('is 44x44', async () => {
    await render(
      <IconButton icon="close" accessibilityLabel="Close" onPress={jest.fn()} testID="ib" />
    );
    const s = StyleSheet.flatten(screen.getByTestId('ib').props.style) as any;
    expect(s.width).toBe(44);
    expect(s.height).toBe(44);
  });

  it('calls onPress', async () => {
    const onPress = jest.fn();
    await render(<IconButton icon="close" accessibilityLabel="Close" onPress={onPress} />);
    await fireEvent.press(screen.getByRole('button', { name: 'Close' }));
    expect(onPress).toHaveBeenCalledTimes(1);
  });

  it('disabled: announces disabled and ignores presses', async () => {
    const onPress = jest.fn();
    await render(
      <IconButton icon="close" accessibilityLabel="Close" onPress={onPress} disabled testID="ib" />
    );
    const el = screen.getByTestId('ib');
    expect(el.props.accessibilityState).toMatchObject({ disabled: true });
    await fireEvent.press(el);
    expect(onPress).not.toHaveBeenCalled();
  });

  it('requires accessibilityLabel at the type level', () => {
    // @ts-expect-error accessibilityLabel is required
    const bad = <IconButton icon="close" onPress={jest.fn()} />;
    expect(bad).toBeTruthy();
  });

  it('dims when pressed and when disabled', async () => {
    const style = (id: string) => StyleSheet.flatten(screen.getByTestId(id).props.style) as any;
    const { rerender } = await render(
      <IconButton icon="close" accessibilityLabel="Close" onPress={jest.fn()} testID="ib" />
    );
    expect(style('ib').opacity).toBe(1);
    await pressIn(screen.getByTestId('ib'));
    expect(style('ib').opacity).toBe(0.6);
    await rerender(
      <IconButton
        icon="close"
        accessibilityLabel="Close"
        onPress={jest.fn()}
        disabled
        testID="ib"
      />
    );
    expect(style('ib').opacity).toBe(0.4);
  });
});
