import { SessionLogger, redact } from '../../components/FeedbackWidget/SessionLogger';

/** A JWT-shaped string for redaction tests, assembled at runtime. */
function fakeJwt(payload: string, signature: string): string {
  const b64 = (text: string) => Buffer.from(text).toString('base64url');
  return [b64('{"alg":"RS256"}'), b64(payload), b64(signature)].join('.');
}

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
      console.warn('auth failed with Bearer abc.DEF-123_xyz== end');
      const logs = SessionLogger.getLogs();
      expect(logs).toMatch(/Bearer \[redacted\] end/);
      expect(logs).not.toMatch(/abc\.DEF/);
    });

    it('redacts JWT-shaped strings', () => {
      SessionLogger.init();
      console.error(`token ${fakeJwt('{"sub":"123"}', 'signature')} ok`);
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

    it('redacts Bearer case-insensitively, keeping the keyword case, only for token-like values', () => {
      SessionLogger.init();
      console.warn('BEARER abcdefgh12345 and bearer of bad news');
      const logs = SessionLogger.getLogs();
      expect(logs).toMatch(/BEARER \[redacted\] and bearer of bad news/);
    });

    it('redacts Authorization headers with any scheme', () => {
      SessionLogger.init();
      console.warn('Authorization: Basic dXNlcjpwYXNz');
      console.warn('authorization: rawtoken123');
      const logs = SessionLogger.getLogs();
      expect(logs).toMatch(/Authorization: \[redacted\]/);
      expect(logs).toMatch(/authorization: \[redacted\]/);
      expect(logs).not.toMatch(/dXNlcjpwYXNz|rawtoken123/);
    });

    it('redacts sensitive URL / query params', () => {
      SessionLogger.init();
      console.warn('GET /x?access_token=abc123&Api_Key=zzz&ok=1 password=hunter2 token=t1');
      const logs = SessionLogger.getLogs();
      expect(logs).toMatch(/access_token=\[redacted\]&Api_Key=\[redacted\]&ok=1/);
      expect(logs).toMatch(/password=\[redacted\] token=\[redacted\]/);
      expect(logs).not.toMatch(/abc123|zzz|hunter2|t1\b/);
    });

    it('redacts emails with an apostrophe in the local part', () => {
      SessionLogger.init();
      console.warn("mail o'brien@example.com now");
      expect(SessionLogger.getLogs()).toMatch(/mail \[email\] now/);
    });

    it('redacts a JWT straddling the 1,000-char cut', () => {
      SessionLogger.init();
      // Built at runtime so no JWT literal sits in the source (gitleaks).
      const jwt = fakeJwt('{"sub":"SECRETPAYLOAD","exp":1}', 'signaturesignature');
      console.warn('a'.repeat(980) + ' ' + jwt);
      const logs = SessionLogger.getLogs();
      expect(logs).not.toMatch(/eyJ|SRUNSRVRQ|U1NiJ9|c2lnbmF0/);
      expect(logs).toMatch(/\[jwt\]|…\[truncated\]/);
    });

    it('redacts an email cut before its TLD at the 1,000-char boundary', () => {
      SessionLogger.init();
      console.warn('a'.repeat(985) + ' jane.doe@example.com tail');
      const logs = SessionLogger.getLogs();
      expect(logs).not.toMatch(/jane|doe|@|exam/);
    });

    it('drops a partial token left at the cut when redaction did not match', () => {
      SessionLogger.init();
      console.warn('a'.repeat(990) + ' ab12.cd34ef56gh78ij90');
      const logs = SessionLogger.getLogs();
      expect(logs).not.toMatch(/ab12/);
      expect(logs).toMatch(/…\[truncated\]/);
    });

    it.each([
      ['a repeated', 'a'.repeat(4000)],
      ['a. repeated', 'a.'.repeat(2000)],
      ['eyJ repeated', 'eyJ'.repeat(1333)],
      ['eyJ. repeated', 'eyJ.'.repeat(1000)],
      ['at signs', 'a@'.repeat(2000)],
      ['domain run', 'a@' + 'b.'.repeat(1998)],
      ['Bearer spaces', 'Bearer' + ' '.repeat(3990)],
      ['Authorization spaces', 'Authorization:' + ' '.repeat(3980)],
    ])('redact() is linear on pathological input: %s', (_name, input) => {
      redact(input); // warm up the JIT
      // Best of 5 so a GC pause or scheduler hiccup cannot fail the test; the
      // old quadratic regexes took 7-21 ms on these inputs every time.
      let best = Infinity;
      for (let i = 0; i < 5; i++) {
        const start = performance.now();
        redact(input);
        best = Math.min(best, performance.now() - start);
      }
      expect(best).toBeLessThan(5);
    });
  });

  describe('bounded serialisation (#758)', () => {
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

    it('serialises a 200k-element array quickly and bounded', () => {
      SessionLogger.init();
      const big = new Array(200000).fill(1);
      const start = Date.now();
      console.warn(big);
      expect(Date.now() - start).toBeLessThan(100);
      const line = SessionLogger.getLogs();
      expect(line.length).toBeLessThan(1200);
      expect(line).toMatch(/\+199900/);
    });

    it('bounds a huge object with many keys', () => {
      SessionLogger.init();
      const obj: Record<string, string> = {};
      for (let i = 0; i < 20000; i++) obj[`k${i}`] = 'v'.repeat(50);
      console.warn(obj);
      expect(SessionLogger.getLogs().length).toBeLessThan(1200);
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

    it('counts lone surrogates as 3 bytes and keeps getLogs() within 100 KB', () => {
      SessionLogger.init();
      const unit = '\ud800€';
      console.warn(unit.repeat(100));
      const line = SessionLogger.getLogs();
      const msg = line.slice(line.indexOf('WARN ') + 5);
      // 100 x (lone surrogate 3 bytes + euro 3 bytes) = 600 bytes
      expect(msg.length).toBe(200);
      expect(Buffer.byteLength(msg, 'utf8')).toBe(
        Buffer.byteLength(msg.replace(/\ud800/g, '�'), 'utf8')
      );
      expect(Buffer.byteLength(msg.replace(/\ud800/g, '�'), 'utf8')).toBe(600);
      for (let i = 0; i < 150; i++) console.warn(unit.repeat(160));
      const logs = SessionLogger.getLogs();
      expect(Buffer.byteLength(logs.replace(/\ud800/g, '�'), 'utf8')).toBeLessThanOrEqual(
        100 * 1024
      );
    });
  });
});
