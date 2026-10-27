import fs from 'fs';
import path from 'path';

const mockInit = jest.fn();
const mockAddBreadcrumb = jest.fn();
const mockCaptureException = jest.fn();
const mockWrap = jest.fn((c: unknown) => c);

jest.mock('@sentry/react-native', () => ({
  init: mockInit,
  addBreadcrumb: mockAddBreadcrumb,
  captureException: mockCaptureException,
  wrap: mockWrap,
}));

beforeEach(() => {
  jest.resetModules();
  mockInit.mockReset();
  mockAddBreadcrumb.mockReset();
  delete process.env.EXPO_PUBLIC_SENTRY_DSN;
  delete process.env.EXPO_PUBLIC_ENVIRONMENT;
});

describe('initSentry', () => {
  it('calls Sentry.init with correct config when DSN is set', () => {
    process.env.EXPO_PUBLIC_SENTRY_DSN = 'https://example@sentry.io/123';
    process.env.EXPO_PUBLIC_ENVIRONMENT = 'staging';

    const { initSentry } = require('../../lib/sentry');
    // Reset mocks after module self-init, then call manually
    mockInit.mockClear();
    mockAddBreadcrumb.mockClear();

    initSentry();

    expect(mockInit).toHaveBeenCalledWith({
      dsn: 'https://example@sentry.io/123',
      environment: 'staging',
      tracesSampleRate: 1.0,
      sendDefaultPii: false,
      enabled: true,
      beforeSend: expect.any(Function),
    });
  });

  it('does not call Sentry.init when DSN is missing', () => {
    const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});

    const { initSentry } = require('../../lib/sentry');
    mockInit.mockClear();

    initSentry();

    expect(mockInit).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalledWith('[Sentry] No DSN configured, skipping init');
    warn.mockRestore();
  });

  it('catches and logs init errors without propagating', () => {
    process.env.EXPO_PUBLIC_SENTRY_DSN = 'https://example@sentry.io/123';
    const error = new Error('init boom');
    mockInit.mockImplementation(() => {
      throw error;
    });

    const consoleError = jest.spyOn(console, 'error').mockImplementation(() => {});

    const { initSentry } = require('../../lib/sentry');
    mockInit.mockClear();
    mockInit.mockImplementation(() => {
      throw error;
    });

    expect(() => initSentry()).not.toThrow();
    expect(consoleError).toHaveBeenCalledWith('[Sentry] init failed:', error);
    consoleError.mockRestore();
  });

  it('uses tracesSampleRate 0.2 in production', () => {
    process.env.EXPO_PUBLIC_SENTRY_DSN = 'https://example@sentry.io/123';
    process.env.EXPO_PUBLIC_ENVIRONMENT = 'production';

    const { initSentry } = require('../../lib/sentry');
    mockInit.mockClear();

    initSentry();

    expect(mockInit).toHaveBeenCalledWith(expect.objectContaining({ tracesSampleRate: 0.2 }));
  });

  it('uses tracesSampleRate 1.0 in non-production', () => {
    process.env.EXPO_PUBLIC_SENTRY_DSN = 'https://example@sentry.io/123';
    process.env.EXPO_PUBLIC_ENVIRONMENT = 'development';

    const { initSentry } = require('../../lib/sentry');
    mockInit.mockClear();

    initSentry();

    expect(mockInit).toHaveBeenCalledWith(expect.objectContaining({ tracesSampleRate: 1.0 }));
  });

  it('adds lifecycle breadcrumb after successful init', () => {
    process.env.EXPO_PUBLIC_SENTRY_DSN = 'https://example@sentry.io/123';
    process.env.EXPO_PUBLIC_ENVIRONMENT = 'staging';

    const { initSentry } = require('../../lib/sentry');
    mockAddBreadcrumb.mockClear();

    initSentry();

    expect(mockAddBreadcrumb).toHaveBeenCalledWith({
      category: 'app.lifecycle',
      message: 'Sentry init completed',
      level: 'info',
      data: { environment: 'staging' },
    });
  });
});

describe('import order guard', () => {
  it('_layout.tsx imports sentry before any other module', () => {
    const layoutPath = path.resolve(__dirname, '../../app/_layout.tsx');
    const content = fs.readFileSync(layoutPath, 'utf-8');
    const importLines = content.split('\n').filter((line) => /^import\s/.test(line));

    expect(importLines.length).toBeGreaterThan(0);
    expect(importLines[0]).toMatch(/from\s+['"]\.\.\/lib\/sentry['"]/);
  });
});
