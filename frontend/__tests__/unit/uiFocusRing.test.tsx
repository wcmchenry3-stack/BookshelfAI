import React from 'react';
import { Platform, StyleSheet } from 'react-native';
import { act, fireEvent, render, screen } from '@testing-library/react-native';

import { Button, Chip, IconButton, ListRow, SearchField } from '../../components/ui';
import { TonalChip } from '../../components/library/TonalChip';
import { lightTheme } from '../../theme';

// Fake `document` so the web modality listeners (installed on first use) can be driven.
const listeners: Record<string, (e: unknown) => void> = {};
const keyboard = () => listeners.keydown({ key: 'Tab' });
const pointer = () => listeners.pointerdown({});

const outline = (el: { props: { style?: unknown } }) =>
  StyleSheet.flatten(el.props.style as any)?.outlineStyle;
const ring = (el: { props: { style?: unknown } }) => StyleSheet.flatten(el.props.style as any);

const originalOS = Platform.OS;
beforeAll(() => {
  (global as any).document = {
    addEventListener: (type: string, fn: (e: unknown) => void) => {
      listeners[type] = fn;
    },
  };
});
afterAll(() => {
  delete (global as any).document;
});
beforeEach(() => {
  Platform.OS = 'web';
});
afterEach(() => {
  Platform.OS = originalOS;
});

describe('keyboard focus ring (web)', () => {
  it('Button shows a 2px primary ring on keyboard focus and drops it on blur', async () => {
    await render(<Button label="Save" onPress={jest.fn()} testID="b" />);
    const b = screen.getByTestId('b');
    expect(outline(b)).toBeUndefined();
    await act(async () => keyboard());
    await fireEvent(b, 'focus');
    const s = ring(screen.getByTestId('b'));
    expect(s.outlineStyle).toBe('solid');
    expect(s.outlineWidth).toBeGreaterThanOrEqual(2);
    expect(s.outlineColor).toBe(lightTheme.colors.primary);
    expect(s.outlineOffset).toBe(2);
    await fireEvent(screen.getByTestId('b'), 'blur');
    expect(outline(screen.getByTestId('b'))).toBeUndefined();
  });

  it('Button shows no ring when focus follows a pointer press', async () => {
    await render(<Button label="Save" onPress={jest.fn()} testID="b" />);
    await act(async () => pointer());
    await fireEvent(screen.getByTestId('b'), 'focus');
    expect(outline(screen.getByTestId('b'))).toBeUndefined();
  });

  it('SearchField shows the ring on its field on keyboard focus and removes it on blur', async () => {
    await render(
      <SearchField
        value=""
        onChangeText={jest.fn()}
        placeholder="Search"
        accessibilityLabel="Search books"
        clearAccessibilityLabel="Clear"
        testID="s"
      />
    );
    const input = screen.getByTestId('s');
    await act(async () => keyboard());
    await fireEvent(input, 'focus');
    const field = () => screen.getByTestId('s-field');
    expect(outline(field())).toBe('solid');
    expect(ring(field()).outlineWidth).toBeGreaterThanOrEqual(2);
    await fireEvent(input, 'blur');
    expect(outline(field())).toBeUndefined();
  });

  it('SearchField also shows the ring when focused by pointer (text inputs show focus on click)', async () => {
    await render(
      <SearchField
        value=""
        onChangeText={jest.fn()}
        placeholder="Search"
        accessibilityLabel="Search books"
        clearAccessibilityLabel="Clear"
        testID="s"
      />
    );
    await act(async () => pointer());
    const input = screen.getByTestId('s');
    await fireEvent(input, 'focus');
    expect(outline(screen.getByTestId('s-field'))).toBe('solid');
  });

  it('IconButton, Chip, ListRow and TonalChip share the ring', async () => {
    await render(
      <>
        <IconButton icon="add" onPress={jest.fn()} accessibilityLabel="Add" testID="ib" />
        <Chip label="All" onPress={jest.fn()} testID="chip" />
        <ListRow label="Row" onPress={jest.fn()} testID="row" />
        <TonalChip
          label="Bought"
          icon="check"
          accessibilityLabel="Bought"
          onPress={jest.fn()}
          testID="tc"
        />
      </>
    );
    await act(async () => keyboard());
    for (const id of ['ib', 'chip', 'row', 'tc']) {
      await fireEvent(screen.getByTestId(id), 'focus');
      expect(outline(screen.getByTestId(id))).toBe('solid');
    }
    // Rows sit in a clipping card, so their ring is drawn inside.
    expect(ring(screen.getByTestId('row')).outlineOffset).toBe(-2);
  });

  it('draws no ring off web', async () => {
    Platform.OS = 'ios';
    await render(<Button label="Save" onPress={jest.fn()} testID="b" />);
    await fireEvent(screen.getByTestId('b'), 'focus');
    expect(outline(screen.getByTestId('b'))).toBeUndefined();
  });
});
