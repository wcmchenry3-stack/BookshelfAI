import React from 'react';
import { StyleSheet } from 'react-native';
import { fireEvent, render, screen } from '@testing-library/react-native';

import { SearchField } from '../../components/ui';
import { lightTheme } from '../../theme';

const props = {
  placeholder: 'Search books',
  accessibilityLabel: 'Search your library',
  clearAccessibilityLabel: 'Clear search',
};

describe('SearchField', () => {
  it('labels the input and shows the placeholder', async () => {
    await render(<SearchField value="" onChangeText={jest.fn()} {...props} />);
    expect(screen.getByLabelText('Search your library')).toBeTruthy();
    expect(screen.getByPlaceholderText('Search books')).toBeTruthy();
  });

  it('is 40px tall, radius 10, field fill with a 1px outlineField border', async () => {
    await render(<SearchField value="" onChangeText={jest.fn()} {...props} testID="s" />);
    const wrap = screen.getByTestId('s').parent as any;
    const s = StyleSheet.flatten(wrap.props.style) as any;
    expect(s.height).toBe(40);
    expect(s.borderRadius).toBe(10);
    expect(s.borderWidth).toBe(1);
    expect(s.backgroundColor).toBe(lightTheme.colors.field);
    expect(s.borderColor).toBe(lightTheme.colors.outlineField);
  });

  it('forwards typing and submit', async () => {
    const onChangeText = jest.fn();
    const onSubmitEditing = jest.fn();
    await render(
      <SearchField
        value=""
        onChangeText={onChangeText}
        onSubmitEditing={onSubmitEditing}
        {...props}
      />
    );
    const input = screen.getByLabelText('Search your library');
    await fireEvent.changeText(input, 'dune');
    expect(onChangeText).toHaveBeenCalledWith('dune');
    await fireEvent(input, 'submitEditing');
    expect(onSubmitEditing).toHaveBeenCalled();
  });

  it('hides the clear button when empty', async () => {
    await render(<SearchField value="" onChangeText={jest.fn()} {...props} />);
    expect(screen.queryByRole('button', { name: 'Clear search' })).toBeNull();
  });

  it('shows an accessible clear button that empties the value', async () => {
    const onChangeText = jest.fn();
    await render(<SearchField value="dune" onChangeText={onChangeText} {...props} />);
    await fireEvent.press(screen.getByRole('button', { name: 'Clear search' }));
    expect(onChangeText).toHaveBeenCalledWith('');
  });
});
