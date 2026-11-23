import { AccessibilityInfo } from 'react-native';
import { act, renderHook } from '@testing-library/react-native';

import { useReducedMotion } from '../../hooks/useReducedMotion';

describe('useReducedMotion (#1063)', () => {
  it('follows the OS Reduce Motion setting and its change events', async () => {
    let fire: (on: boolean) => void = () => {};
    jest.spyOn(AccessibilityInfo, 'isReduceMotionEnabled').mockResolvedValue(true);
    jest.spyOn(AccessibilityInfo, 'addEventListener').mockImplementation(((
      _name: string,
      cb: (on: boolean) => void
    ) => {
      fire = cb;
      return { remove: jest.fn() };
    }) as never);

    const { result } = await renderHook(() => useReducedMotion());
    await act(async () => {});
    expect(result.current).toBe(true);

    await act(async () => fire(false));
    expect(result.current).toBe(false);
  });
});
