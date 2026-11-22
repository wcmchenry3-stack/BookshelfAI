import React from 'react';
import { StyleSheet } from 'react-native';
import { fireEvent, render } from '@testing-library/react-native';

import { BoughtChip } from '../../components/library/BoughtChip';

jest.mock('../../hooks/useTheme', () => require('../helpers/screenMocks').themeModule());

function chip(props: Partial<React.ComponentProps<typeof BoughtChip>> = {}) {
  return (
    <BoughtChip
      label="Bought"
      accessibilityLabel="Mark Dune as bought"
      onPress={() => {}}
      testID="chip"
      {...props}
    />
  );
}

describe('BoughtChip (#929)', () => {
  it('is a button with a 44px touch target around a 32px pill', async () => {
    const { getByTestId, getByText } = await render(chip());
    const target = getByTestId('chip');
    expect(target.props.accessibilityRole).toBe('button');
    expect(target.props.accessibilityLabel).toBe('Mark Dune as bought');
    const targetStyle = StyleSheet.flatten(
      typeof target.props.style === 'function'
        ? target.props.style({ pressed: false })
        : target.props.style
    );
    expect(targetStyle.minHeight).toBeGreaterThanOrEqual(44);
    expect(targetStyle.minWidth).toBeGreaterThanOrEqual(44);
    const pill = getByText('Bought').parent!;
    expect(StyleSheet.flatten(pill.props.style).height).toBe(32);
  });

  it('presses when enabled', async () => {
    const onPress = jest.fn();
    const { getByTestId } = await render(chip({ onPress }));
    await fireEvent.press(getByTestId('chip'));
    expect(onPress).toHaveBeenCalledTimes(1);
  });

  it('is disabled and does not fire when disabled', async () => {
    const onPress = jest.fn();
    const { getByTestId } = await render(chip({ onPress, disabled: true }));
    expect(getByTestId('chip').props.accessibilityState.disabled).toBe(true);
    await fireEvent.press(getByTestId('chip'));
    expect(onPress).not.toHaveBeenCalled();
  });
});
