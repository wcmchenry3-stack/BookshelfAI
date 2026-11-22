/**
 * #817 / CLAUDE.md rule 13: `../lib/sentry` must be the first import of
 * app/_layout.tsx so Sentry self-initialises before any other module runs.
 * The layout's own start-up code (SessionLogger.init + lifecycle breadcrumb)
 * sits below the imports so `import/first` passes without a disable.
 *
 * Babel hoists imports to requires at the top of the file, so runtime order
 * cannot tell these layouts apart; the check is on the source structure.
 */
import fs from 'fs';
import path from 'path';

const lines = fs.readFileSync(path.join(__dirname, '../../app/_layout.tsx'), 'utf8').split('\n');

// An import statement starts with `import` or, for a multi-line import, ends
// with `} from '...'` at column 0.
const isImportLine = (line: string) => /^import\b/.test(line) || /^\} from /.test(line);

describe('app/_layout.tsx start-up order (source structure)', () => {
  it('keeps ../lib/sentry as the first import (rule 13)', () => {
    const firstImport = lines.find((line) => /^import\b/.test(line));
    expect(firstImport).toContain("from '../lib/sentry'");
  });

  it('runs SessionLogger.init and the lifecycle breadcrumb after the last import', () => {
    const lastImport = lines.map(isImportLine).lastIndexOf(true);
    const init = lines.findIndex((line) => line.startsWith('SessionLogger.init('));
    const crumb = lines.findIndex((line) =>
      line.includes("message: 'Root layout module evaluated'")
    );

    expect(lastImport).toBeGreaterThan(0);
    expect(init).toBeGreaterThan(lastImport);
    expect(crumb).toBeGreaterThan(lastImport);
  });
});
