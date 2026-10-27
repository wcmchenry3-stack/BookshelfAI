import { AxiosError } from 'axios';
import { filterExpectedNetworkNoise, isExpectedClientNetworkNoise } from '../../lib/sentryFilter';

const MECH = { type: 'auto.browser.global_handlers.onunhandledrejection', handled: false };
const BUNDLE = { filename: 'app:///_expo/static/js/web/entry-abc123.js' };
const DOM_NETWORK_ERROR = { originalException: { name: 'NetworkError', code: 19 } };

function ev(value: Record<string, unknown>) {
  return { exception: { values: [{ mechanism: MECH, ...value }] } };
}

describe('isExpectedClientNetworkNoise', () => {
  it('drops the font-load timeout with only bundled-entry frames', () => {
    const e = ev({
      type: 'Error',
      value: '12000ms timeout exceeded',
      stacktrace: { frames: [BUNDLE, { filename: '<anonymous>' }] },
    });
    expect(isExpectedClientNetworkNoise(e)).toBe(true);
    expect(filterExpectedNetworkNoise(e)).toBeNull();
  });

  it('drops the font-load timeout with no stacktrace', () => {
    expect(
      isExpectedClientNetworkNoise(ev({ type: 'Error', value: '12000ms timeout exceeded' }))
    ).toBe(true);
  });

  it('drops a bare NetworkError DOMException without a stack', () => {
    expect(
      isExpectedClientNetworkNoise(ev({ type: 'NetworkError', value: 'A network error occurred.' }))
    ).toBe(true);
    expect(
      isExpectedClientNetworkNoise(
        ev({ type: 'Error', value: 'NetworkError: A network error occurred.' })
      )
    ).toBe(true);
    expect(
      isExpectedClientNetworkNoise(
        ev({ type: 'Error', value: 'whatever', stacktrace: { frames: [] } }),
        DOM_NETWORK_ERROR
      )
    ).toBe(true);
  });

  it('keeps an API-client (axios) network error and axios timeout', () => {
    const err = new AxiosError('Network Error', 'ERR_NETWORK');
    const e = ev({ type: err.name, value: err.message, stacktrace: { frames: [BUNDLE] } });
    expect(isExpectedClientNetworkNoise(e, { originalException: err })).toBe(false);
    const timeout = new AxiosError('timeout of 30000ms exceeded', 'ECONNABORTED');
    expect(isExpectedClientNetworkNoise(ev({ type: timeout.name, value: timeout.message }))).toBe(
      false
    );
  });

  it('keeps a NetworkError with a stack frame', () => {
    const e = ev({
      type: 'NetworkError',
      value: 'A network error occurred.',
      stacktrace: { frames: [BUNDLE] },
    });
    expect(isExpectedClientNetworkNoise(e)).toBe(false);
  });

  it('keeps the font timeout when a first-party frame is present', () => {
    const e = ev({
      type: 'Error',
      value: '12000ms timeout exceeded',
      stacktrace: { frames: [BUNDLE, { filename: 'app:///lib/fonts.ts' }] },
    });
    expect(isExpectedClientNetworkNoise(e)).toBe(false);
    expect(filterExpectedNetworkNoise(e)).toBe(e);
  });

  it('keeps unrelated errors and other timeouts', () => {
    expect(isExpectedClientNetworkNoise(ev({ type: 'TypeError', value: 'x is undefined' }))).toBe(
      false
    );
    expect(
      isExpectedClientNetworkNoise(ev({ type: 'Error', value: 'Request timed out after 30000ms' }))
    ).toBe(false);
    expect(
      isExpectedClientNetworkNoise(ev({ type: 'Error', value: 'timeout of 12000ms exceeded' }))
    ).toBe(false);
  });

  it('keeps matching signatures that are not unhandled rejections', () => {
    const e = {
      exception: {
        values: [
          {
            type: 'NetworkError',
            value: 'A network error occurred.',
            mechanism: { type: 'generic' },
          },
        ],
      },
    };
    expect(isExpectedClientNetworkNoise(e)).toBe(false);
  });

  it('keeps malformed events without throwing', () => {
    const odd = [
      {},
      null,
      undefined,
      'str',
      { exception: {} },
      { exception: { values: 'nope' } },
      { exception: { values: [null] } },
      ev({ type: 'Error', value: '12000ms timeout exceeded', stacktrace: { frames: 'bad' } }),
    ];
    for (const o of odd) {
      expect(() => isExpectedClientNetworkNoise(o)).not.toThrow();
      expect(isExpectedClientNetworkNoise(o)).toBe(false);
      expect(filterExpectedNetworkNoise(o)).toBe(o);
    }
  });

  it('calls onDrop only when dropping, and never throws from it', () => {
    const drop = jest.fn(() => {
      throw new Error('boom');
    });
    const noise = ev({ type: 'NetworkError', value: 'A network error occurred.' });
    expect(filterExpectedNetworkNoise(noise, undefined, drop)).toBeNull();
    expect(drop).toHaveBeenCalledTimes(1);
    filterExpectedNetworkNoise({}, undefined, drop);
    expect(drop).toHaveBeenCalledTimes(1);
  });
});
