import { SessionLogger } from '../../components/FeedbackWidget/SessionLogger';

beforeEach(() => {
  SessionLogger._reset();
});

afterEach(() => {
  SessionLogger._reset();
});

describe('SessionLogger', () => {
  describe('init()', () => {
    it('patches console.warn and captures entries', () => {
      SessionLogger.init();
      console.warn('hello warn');
      expect(SessionLogger.size).toBe(1);
      const logs = SessionLogger.getLogs();
      expect(logs).toMatch(/WARN hello warn/);
    });

    it('patches console.error and captures entries', () => {
      SessionLogger.init();
      console.error('boom');
      expect(SessionLogger.size).toBe(1);
      expect(SessionLogger.getLogs()).toMatch(/ERROR boom/);
    });

    it('is idempotent — calling init() twice does not double-patch', () => {
      SessionLogger.init();
      SessionLogger.init();
      console.warn('once');
      expect(SessionLogger.size).toBe(1);
    });

    it('serialises Error objects as "Name: message"', () => {
      SessionLogger.init();
      console.error(new Error('something broke'));
      expect(SessionLogger.getLogs()).toMatch(/Error: something broke/);
    });

    it('serialises non-string non-Error args via JSON.stringify', () => {
      SessionLogger.init();
      console.warn({ code: 42 });
      expect(SessionLogger.getLogs()).toMatch(/\{"code":42\}/);
    });

    it('joins multiple args with a space', () => {
      SessionLogger.init();
      console.warn('a', 'b', 'c');
      expect(SessionLogger.getLogs()).toMatch(/WARN a b c/);
    });
  });

  describe('circular buffer', () => {
    it('wraps at MAX_ENTRIES (200) — oldest entry is dropped', () => {
      SessionLogger.init();
      for (let i = 0; i < 201; i++) {
        console.warn(`msg-${i}`);
      }
      expect(SessionLogger.size).toBe(200);
      // msg-0 has been evicted; msg-1 is now the oldest
      expect(SessionLogger.getLogs()).not.toMatch(/WARN msg-0\b/);
      expect(SessionLogger.getLogs()).toMatch(/WARN msg-1\b/);
      expect(SessionLogger.getLogs()).toMatch(/WARN msg-200\b/);
    });
  });

  describe('getLogs()', () => {
    it('returns empty string when buffer is empty', () => {
      expect(SessionLogger.getLogs()).toBe('');
    });

    it('includes ISO timestamps', () => {
      SessionLogger.init();
      console.warn('ts-test');
      expect(SessionLogger.getLogs()).toMatch(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
    });

    it('lists entries oldest-first (bottom is most recent)', () => {
      SessionLogger.init();
      console.warn('first');
      console.warn('second');
      const lines = SessionLogger.getLogs().split('\n');
      expect(lines[0]).toMatch(/first/);
      expect(lines[1]).toMatch(/second/);
    });
  });

  describe('_reset()', () => {
    it('clears the buffer', () => {
      SessionLogger.init();
      console.warn('x');
      SessionLogger._reset();
      expect(SessionLogger.size).toBe(0);
      expect(SessionLogger.getLogs()).toBe('');
    });

    it('allows re-initialisation after reset', () => {
      SessionLogger.init();
      SessionLogger._reset();
      SessionLogger.init();
      console.warn('after reset');
      expect(SessionLogger.size).toBe(1);
    });
  });

  describe('robustness (#758)', () => {
    let spies: jest.SpyInstance[] = [];
    beforeEach(() => {
      spies = [
        jest.spyOn(console, 'warn').mockImplementation(() => {}),
        jest.spyOn(console, 'error').mockImplementation(() => {}),
      ];
    });
    afterEach(() => {
      SessionLogger._reset();
      spies.forEach((sp) => sp.mockRestore());
    });

    it('does not throw on circular objects and still calls the original console', () => {
      const spy = spies[0];
      SessionLogger.init();
      const circ: Record<string, unknown> = {};
      circ.self = circ;
      expect(() => console.warn(circ)).not.toThrow();
      expect(spy).toHaveBeenCalledWith(circ);
      expect(SessionLogger.getLogs()).toMatch(/\[unserializable\]/);
    });

    it('handles BigInt without throwing', () => {
      const spy = spies[1];
      SessionLogger.init();
      expect(() => console.error(BigInt(123))).not.toThrow();
      expect(spy).toHaveBeenCalledWith(BigInt(123));
      expect(SessionLogger.size).toBe(1);
    });

    it('handles throwing getters', () => {
      const spy = spies[0];
      SessionLogger.init();
      const bad = {
        get boom(): string {
          throw new Error('nope');
        },
      };
      expect(() => console.warn(bad)).not.toThrow();
      expect(spy).toHaveBeenCalledWith(bad);
      expect(SessionLogger.getLogs()).toMatch(/\[unserializable\]/);
    });
  });

  describe('redaction (#758)', () => {
    let spies: jest.SpyInstance[] = [];
    beforeEach(() => {
      spies = [
        jest.spyOn(console, 'warn').mockImplementation(() => {}),
        jest.spyOn(console, 'error').mockImplementation(() => {}),
      ];
    });
    afterEach(() => {
      SessionLogger._reset();
      spies.forEach((sp) => sp.mockRestore());
    });

    it('redacts Bearer tokens', () => {
      SessionLogger.init();
      console.warn('auth failed Authorization: Bearer abc.DEF-123_xyz== end');
      const logs = SessionLogger.getLogs();
      expect(logs).toMatch(/Bearer \[redacted\] end/);
      expect(logs).not.toMatch(/abc\.DEF/);
    });

    it('redacts JWT-shaped strings', () => {
      SessionLogger.init();
      console.error('token eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl ok');
      const logs = SessionLogger.getLogs();
      expect(logs).toMatch(/token \[jwt\] ok/);
      expect(logs).not.toMatch(/eyJ/);
    });

    it('redacts email addresses', () => {
      SessionLogger.init();
      console.warn('user jane.doe+x@example.co.uk failed');
      const logs = SessionLogger.getLogs();
      expect(logs).toMatch(/user \[email\] failed/);
      expect(logs).not.toMatch(/jane/);
    });

    it('completes quickly on adversarial input', () => {
      SessionLogger.init();
      const inputs = [
        'a'.repeat(200000),
        'eyJ'.repeat(100000),
        'Bearer ' + 'a.'.repeat(100000),
        'a@' + 'b.'.repeat(100000),
        ' '.repeat(200000) + 'Bearer',
      ];
      const start = Date.now();
      for (const input of inputs) console.warn(input);
      expect(Date.now() - start).toBeLessThan(1000);
    });
  });

  describe('size caps (#671)', () => {
    let spies: jest.SpyInstance[] = [];
    beforeEach(() => {
      spies = [
        jest.spyOn(console, 'warn').mockImplementation(() => {}),
        jest.spyOn(console, 'error').mockImplementation(() => {}),
      ];
    });
    afterEach(() => {
      SessionLogger._reset();
      spies.forEach((sp) => sp.mockRestore());
    });

    it('caps a 5,000-char line at 1,000 chars plus the marker', () => {
      SessionLogger.init();
      console.warn('x'.repeat(5000));
      const line = SessionLogger.getLogs().split('\n')[0];
      const msg = line.slice(line.indexOf('WARN ') + 5);
      expect(msg).toBe('x'.repeat(1000) + '…[truncated]');
    });

    it('keeps getLogs() within 100 KB, dropping oldest lines first', () => {
      SessionLogger.init();
      for (let i = 0; i < 200; i++) {
        console.warn(`${String(i).padStart(3, '0')}` + 'y'.repeat(997));
      }
      expect(SessionLogger.size).toBe(200);
      const logs = SessionLogger.getLogs();
      expect(Buffer.byteLength(logs, 'utf8')).toBeLessThanOrEqual(100 * 1024);
      expect(logs).toMatch(/WARN 199y/);
      expect(logs).not.toMatch(/WARN 000y/);
      const first = Number(/WARN (\d{3})y/.exec(logs)![1]);
      expect(first).toBeGreaterThan(0);
    });

    it('measures multi-byte characters as UTF-8 bytes', () => {
      SessionLogger.init();
      for (let i = 0; i < 100; i++) console.warn('é'.repeat(1000));
      expect(Buffer.byteLength(SessionLogger.getLogs(), 'utf8')).toBeLessThanOrEqual(100 * 1024);
    });
  });
});
