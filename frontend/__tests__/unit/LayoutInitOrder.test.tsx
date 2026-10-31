/**
 * #817 / CLAUDE.md rule 13: `../lib/sentry` must be the first import of
 * app/_layout.tsx so Sentry self-initialises before any other module runs.
 * The layout's own start-up code (SessionLogger.init + lifecycle breadcrumb)
 * sits below the imports, so it must run after Sentry has loaded and after
 * every other import has been evaluated.
 */
import fs from 'fs';
import path from 'path';

const mockOrder: string[] = [];

jest.mock('../../lib/sentry', () => {
  mockOrder.push('sentry:loaded');
  return {
    Sentry: {
      wrap: jest.fn((component: unknown) => component),
      addBreadcrumb: jest.fn((crumb: { message?: string }) => {
        mockOrder.push(`breadcrumb:${crumb.message}`);
      }),
      captureException: jest.fn(),
    },
    initSentry: jest.fn(),
  };
});

jest.mock('../../components/FeedbackWidget/SessionLogger', () => ({
  SessionLogger: {
    init: jest.fn(() => {
      mockOrder.push('SessionLogger.init');
    }),
  },
}));

// A module imported after Sentry; records when it is evaluated.
jest.mock('../../components/ThemeToggleButton', () => {
  mockOrder.push('other-import:loaded');
  return { ThemeToggleButton: () => null };
});

jest.mock('expo-router', () => ({ Stack: Object.assign(() => null, { Screen: () => null }) }));
jest.mock('expo-font', () => ({ useFonts: jest.fn(() => [true]) }));
jest.mock('@expo-google-fonts/noto-serif', () => ({}));
jest.mock('@expo-google-fonts/inter', () => ({}));

describe('app/_layout.tsx start-up order', () => {
  it('loads Sentry before other imports, then runs SessionLogger.init and the breadcrumb', () => {
    require('../../app/_layout');

    const sentry = mockOrder.indexOf('sentry:loaded');
    const other = mockOrder.indexOf('other-import:loaded');
    const logger = mockOrder.indexOf('SessionLogger.init');
    const crumb = mockOrder.indexOf('breadcrumb:Root layout module evaluated');

    expect(sentry).toBe(0);
    expect(other).toBeGreaterThan(sentry);
    expect(logger).toBeGreaterThan(other);
    expect(crumb).toBeGreaterThan(logger);
  });

  it('keeps ../lib/sentry as the first import in the source (rule 13)', () => {
    const src = fs.readFileSync(path.join(__dirname, '../../app/_layout.tsx'), 'utf8');
    const firstImport = src.split('\n').find((line) => /^import\b/.test(line));
    expect(firstImport).toContain("from '../lib/sentry'");
  });
});
