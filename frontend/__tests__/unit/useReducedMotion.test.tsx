import { AccessibilityInfo } from 'react-native';
import { act, renderHook } from '@testing-library/react-native';

type Hook = () => boolean;

// The hook keeps module-level state, so each test loads a fresh copy.
function load(query: Promise<boolean>) {
  let fire: (on: boolean) => void = () => {};
  jest.spyOn(AccessibilityInfo, 'isReduceMotionEnabled').mockReturnValue(query);
  jest.spyOn(AccessibilityInfo, 'addEventListener').mockImplementation(((
    _name: string,
    cb: (on: boolean) => void
  ) => {
    fire = cb;
    return { remove: jest.fn() };
  }) as never);
  let useReducedMotion!: Hook;
  // Share the test renderer's React; only the hook's own module state is fresh.
  const react = require('react');
  jest.isolateModules(() => {
    jest.doMock('react', () => react);
    useReducedMotion = require('../../hooks/useReducedMotion').useReducedMotion;
  });
  return { useReducedMotion, fire: (on: boolean) => fire(on) };
}

afterEach(() => {
  jest.restoreAllMocks();
});

describe('useReducedMotion (#1063)', () => {
  it('follows the OS Reduce Motion setting and its change events', async () => {
    const { useReducedMotion, fire } = load(Promise.resolve(true));
    const { result } = await renderHook(() => useReducedMotion());
    await act(async () => {});
    expect(result.current).toBe(true);

    await act(async () => fire(false));
    expect(result.current).toBe(false);
  });

  it('does not let a late query result overwrite a newer change event', async () => {
    let resolve!: (on: boolean) => void;
    const { useReducedMotion, fire } = load(new Promise<boolean>((r) => (resolve = r)));
    const { result } = await renderHook(() => useReducedMotion());
    await act(async () => fire(true));
    expect(result.current).toBe(true);

    await act(async () => resolve(false));
    expect(result.current).toBe(true);
  });

  it('stays false when the query rejects', async () => {
    const { useReducedMotion } = load(Promise.reject(new Error('no a11y')));
    const { result } = await renderHook(() => useReducedMotion());
    await act(async () => {});
    expect(result.current).toBe(false);
  });
});
