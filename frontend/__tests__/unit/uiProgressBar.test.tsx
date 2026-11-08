import React from 'react';
import { StyleSheet } from 'react-native';
import { render, screen } from '@testing-library/react-native';

import { ProgressBar } from '../../components/ui';
import { lightTheme } from '../../theme';

const fillWidth = () => (StyleSheet.flatten(screen.getByTestId('p-fill').props.style) as any).width;

describe('ProgressBar', () => {
  it('is a progressbar with label and accessibilityValue', async () => {
    await render(
      <ProgressBar value={3} min={0} max={12} accessibilityLabel="Scan progress" testID="p" />
    );
    const bar = screen.getByRole('progressbar', { name: 'Scan progress' });
    expect(bar.props.accessibilityValue).toEqual({ min: 0, max: 12, now: 3 });
  });

  it('fills proportionally in primary over a primarySoft track', async () => {
    await render(<ProgressBar value={25} accessibilityLabel="x" testID="p" />);
    expect(fillWidth()).toBe('25%');
    expect(
      (StyleSheet.flatten(screen.getByTestId('p-fill').props.style) as any).backgroundColor
    ).toBe(lightTheme.colors.primary);
    expect((StyleSheet.flatten(screen.getByTestId('p').props.style) as any).backgroundColor).toBe(
      lightTheme.colors.primarySoft
    );
  });

  it('respects a custom range', async () => {
    await render(<ProgressBar value={15} min={10} max={20} accessibilityLabel="x" testID="p" />);
    expect(fillWidth()).toBe('50%');
  });

  it('clamps out-of-range values', async () => {
    const { rerender } = await render(
      <ProgressBar value={500} accessibilityLabel="x" testID="p" />
    );
    expect(fillWidth()).toBe('100%');
    expect(screen.getByTestId('p').props.accessibilityValue.now).toBe(100);
    await rerender(<ProgressBar value={-5} accessibilityLabel="x" testID="p" />);
    expect(fillWidth()).toBe('0%');
  });

  it('handles an empty range without dividing by zero', async () => {
    await render(<ProgressBar value={1} min={5} max={5} accessibilityLabel="x" testID="p" />);
    expect(fillWidth()).toBe('0%');
  });
});
