/**
 * GestureRoot (#901): gesture-handler's root view on iOS/Android, a plain
 * full-size View on web so the web bundle does not carry gesture-handler,
 * Reanimated and Worklets until a web feature uses a gesture.
 */
import fs from 'fs';
import path from 'path';
import React from 'react';
import { Text } from 'react-native';
import { render } from '@testing-library/react-native';
import { GestureRoot } from '../../components/GestureRoot';
import { GestureRoot as WebGestureRoot } from '../../components/GestureRoot.web';

describe('GestureRoot (#901)', () => {
  it('native: renders its children in a flex: 1 gesture-handler root', async () => {
    const { getByTestId, getByText } = await render(
      <GestureRoot testID="root">
        <Text>inside</Text>
      </GestureRoot>
    );
    expect(getByTestId('root')).toHaveStyle({ flex: 1 });
    expect(getByText('inside')).toBeTruthy();
  });

  it('web: renders a plain View with flex: 1 around its children', async () => {
    const { getByTestId, getByText } = await render(
      <WebGestureRoot testID="root">
        <Text>inside</Text>
      </WebGestureRoot>
    );
    const root = getByTestId('root');
    expect(root.type).toBe('View');
    expect(root).toHaveStyle({ flex: 1 });
    expect(getByText('inside')).toBeTruthy();
  });

  it('web: does not import react-native-gesture-handler', () => {
    const source = fs.readFileSync(
      path.join(__dirname, '../../components/GestureRoot.web.tsx'),
      'utf8'
    );
    expect(source).not.toMatch(/from 'react-native-gesture-handler'/);
  });
});
