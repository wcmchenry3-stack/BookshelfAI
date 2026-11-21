import React from 'react';
import { StyleSheet, Text } from 'react-native';
import { fireEvent, render, screen } from '@testing-library/react-native';

import { EmptyState } from '../../components/ui';
import { textVariants } from '../../theme/typography';

describe('EmptyState', () => {
  it('renders a serif title as a header, and the body', async () => {
    await render(<EmptyState title="No books yet" body="Scan a shelf to start." />);
    const title = screen.getByRole('header', { name: 'No books yet' });
    expect((StyleSheet.flatten(title.props.style) as any).fontFamily).toBe(
      textVariants.sheetTitle.fontFamily
    );
    expect(screen.getByText('Scan a shelf to start.')).toBeTruthy();
  });

  it('renders the illustration slot, hidden from screen readers', async () => {
    await render(<EmptyState title="T" illustration={<Text>art</Text>} testID="es" />);
    expect(screen.getByText('art', { includeHiddenElements: true })).toBeTruthy();
    expect(
      screen.getByTestId('es-illustration', { includeHiddenElements: true }).props
        .accessibilityElementsHidden
    ).toBe(true);
  });

  it('fires the primary and secondary actions', async () => {
    const primary = jest.fn();
    const secondary = jest.fn();
    await render(
      <EmptyState
        title="T"
        primaryAction={{ label: 'Scan', onPress: primary }}
        secondaryAction={{ label: 'Add manually', onPress: secondary }}
      />
    );
    await fireEvent.press(screen.getByRole('button', { name: 'Scan' }));
    await fireEvent.press(screen.getByRole('button', { name: 'Add manually' }));
    expect(primary).toHaveBeenCalledTimes(1);
    expect(secondary).toHaveBeenCalledTimes(1);
  });

  it('omits optional parts when not given', async () => {
    await render(<EmptyState title="Only title" />);
    expect(screen.queryByRole('button')).toBeNull();
    expect(screen.getByText('Only title')).toBeTruthy();
  });
});
