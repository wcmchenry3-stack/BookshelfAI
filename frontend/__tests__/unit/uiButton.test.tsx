import React from 'react';
import { StyleSheet } from 'react-native';
import { fireEvent, render, screen } from '@testing-library/react-native';

import { Button } from '../../components/ui';
import { ThemeContext, darkTheme, lightTheme } from '../../theme';

const style = (el: { props: { style?: unknown } }) => StyleSheet.flatten(el.props.style as any);

describe('Button', () => {
  it('renders the label with a button role and label', async () => {
    await render(<Button label="Save" onPress={jest.fn()} />);
    expect(screen.getByRole('button', { name: 'Save' })).toBeTruthy();
    expect(screen.getByText('Save')).toBeTruthy();
  });

  it('calls onPress', async () => {
    const onPress = jest.fn();
    await render(<Button label="Save" onPress={onPress} />);
    await fireEvent.press(screen.getByRole('button', { name: 'Save' }));
    expect(onPress).toHaveBeenCalledTimes(1);
  });

  it('primary: 52px tall, radius 12, primary fill, onPrimary text', async () => {
    await render(<Button label="Save" onPress={jest.fn()} testID="b" />);
    const s = style(screen.getByTestId('b'));
    expect(s.height).toBe(52);
    expect(s.borderRadius).toBe(12);
    expect(s.backgroundColor).toBe(lightTheme.colors.primary);
    expect(style(screen.getByText('Save')).color).toBe(lightTheme.colors.onPrimary);
  });

  it('secondary: tonal primarySoft fill with primary text', async () => {
    await render(<Button variant="secondary" label="More" onPress={jest.fn()} testID="b" />);
    expect(style(screen.getByTestId('b')).backgroundColor).toBe(lightTheme.colors.primarySoft);
    expect(style(screen.getByText('More')).color).toBe(lightTheme.colors.primary);
  });

  it('text: transparent, primary text, at least 44px', async () => {
    await render(<Button variant="text" label="Skip" onPress={jest.fn()} testID="b" />);
    const s = style(screen.getByTestId('b'));
    expect(s.backgroundColor).toBe('transparent');
    expect(s.minHeight).toBeGreaterThanOrEqual(44);
    expect(style(screen.getByText('Skip')).color).toBe(lightTheme.colors.primary);
  });

  it('destructive: danger fill with onDanger text', async () => {
    await render(<Button variant="destructive" label="Delete" onPress={jest.fn()} testID="b" />);
    expect(style(screen.getByTestId('b')).backgroundColor).toBe(lightTheme.colors.danger);
    expect(style(screen.getByText('Delete')).color).toBe(lightTheme.colors.onDanger);
  });

  it('follows the dark theme', async () => {
    await render(
      <ThemeContext.Provider value={{ theme: darkTheme, mode: 'dark', toggleTheme: jest.fn() }}>
        <Button label="Save" onPress={jest.fn()} testID="b" />
      </ThemeContext.Provider>
    );
    expect(style(screen.getByTestId('b')).backgroundColor).toBe(darkTheme.colors.primary);
  });

  it('disabled: announces disabled and does not fire onPress', async () => {
    const onPress = jest.fn();
    await render(<Button label="Save" onPress={onPress} disabled testID="b" />);
    const el = screen.getByTestId('b');
    expect(el.props.accessibilityState).toMatchObject({ disabled: true });
    await fireEvent.press(el);
    expect(onPress).not.toHaveBeenCalled();
  });

  it('loading: shows a spinner, announces busy and blocks presses', async () => {
    const onPress = jest.fn();
    await render(<Button label="Save" onPress={onPress} loading testID="b" />);
    expect(screen.getByTestId('b-spinner')).toBeTruthy();
    const el = screen.getByTestId('b');
    expect(el.props.accessibilityState).toMatchObject({ busy: true, disabled: true });
    await fireEvent.press(el);
    expect(onPress).not.toHaveBeenCalled();
  });

  it('renders an optional leading icon and an a11y label override', async () => {
    await render(
      <Button label="Add" icon="add" accessibilityLabel="Add a book" onPress={jest.fn()} />
    );
    expect(screen.getByRole('button', { name: 'Add a book' })).toBeTruthy();
  });
});
