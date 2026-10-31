import { SessionLogger, redact } from '../../components/FeedbackWidget/SessionLogger';

/** A JWT-shaped string for redaction tests, assembled at runtime. */
function fakeJwt(payload: string, signature: string): string {
  const b64 = (text: string) => Buffer.from(text).toString('base64url');
  return [b64('{"alg":"RS256"}'), b64(payload), b64(signature)].join('.');
}

/** The message part of the newest stored line (without timestamp and level). */
function lastMsg(): string {
  const lines = SessionLogger.getLogs().split('\n');
  return lines[lines.length - 1].replace(/^\[[^\]]+\] (?:WARN|ERROR) /, '');
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
      expect(lastMsg()).toBe('a'.repeat(980) + ' [jwt]');
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

  describe('review hardening', () => {
    let spies: jest.SpyInstance[] = [];
    beforeEach(() => {
      spies = [
        jest.spyOn(console, 'warn').mockImplementation(() => {}),
        jest.spyOn(console, 'error').mockImplementation(() => {}),
      ];
      SessionLogger.init();
    });
    afterEach(() => {
      SessionLogger._reset();
      spies.forEach((sp) => sp.mockRestore());
    });

    describe('clip at the 4,000-char work size', () => {
      it('drops a partial email left by the work-size clip and adds the marker', () => {
        console.warn('token=' + 'k'.repeat(3983) + ' jsmith@exa' + 'mple.com');
        expect(lastMsg()).toBe('token=[redacted] …[truncated]');
      });

      it('marks a clipped single string argument as truncated', () => {
        console.warn('token=' + 'k'.repeat(5000));
        expect(lastMsg()).toBe('token=[redacted]…[truncated]');
      });

      it('drops a partial token at the clip of a non-last argument', () => {
        console.warn('y'.repeat(3990) + ' jsmith@exa'.padEnd(11) + 'x'.repeat(100), 'tail');
        const msg = lastMsg();
        expect(msg).not.toMatch(/jsmith/);
        expect(msg).toMatch(/…\[truncated\]/);
      });

      it('does not store text from past the work size and stays fast on 1 MB', () => {
        const start = performance.now();
        console.warn('y'.repeat(3990) + ' jsmith@example.com' + 'z'.repeat(1000000));
        expect(performance.now() - start).toBeLessThan(50);
        const msg = lastMsg();
        expect(msg.length).toBeLessThanOrEqual(1000 + '…[truncated]'.length);
        expect(msg).not.toMatch(/jsmith|zzzz/);
      });
    });

    describe('keyed credentials', () => {
      it.each([
        ['client_secret=s3cr3t', /client_secret=\[redacted\]/, /s3cr3t/],
        ['auth_token=t0k3n', /auth_token=\[redacted\]/, /t0k3n/],
        ['session_token=t0k3n', /session_token=\[redacted\]/, /t0k3n/],
        ['db_password=hunter2', /db_password=\[redacted\]/, /hunter2/],
        ['authToken=t0k3n', /authToken=\[redacted\]/, /t0k3n/],
        ['passwd=hunter2', /passwd=\[redacted\]/, /hunter2/],
        ['x-api-key: k3y', /x-api-key: \[redacted\]/, /k3y/],
        ['refresh_token=r3fr3sh&a=1', /refresh_token=\[redacted\]&a=1/, /r3fr3sh/],
        ['TOKEN=UPPER', /TOKEN=\[redacted\]/, /UPPER/],
      ])('redacts %s', (input, expected, secret) => {
        console.warn(input);
        expect(lastMsg()).toMatch(expected);
        expect(lastMsg()).not.toMatch(secret);
      });

      it('leaves similar words alone', () => {
        console.warn('tokenizer = fast, max_tokens 5, secretive');
        expect(lastMsg()).toBe('tokenizer = fast, max_tokens 5, secretive');
      });
    });

    describe('JSON-shaped and quoted credentials', () => {
      it('redacts a JSON Authorization header value', () => {
        console.error({ Authorization: 'Basic zzzzzz' });
        expect(lastMsg()).toBe('{"Authorization":"[redacted]"}');
      });

      it('redacts a JSON password', () => {
        console.error({ password: 'hunter2', user: 'u' });
        expect(lastMsg()).toBe('{"password":"[redacted]","user":"u"}');
      });

      it('redacts a JSON token', () => {
        console.error({ token: 'abc123' });
        expect(lastMsg()).toBe('{"token":"[redacted]"}');
      });

      it('redacts a single-quoted Authorization value', () => {
        console.warn("Authorization: 'Basic abc'");
        expect(lastMsg()).toBe("Authorization: '[redacted]'");
      });

      it('redacts authorization=Basic abc', () => {
        console.warn('authorization=Basic abc');
        expect(lastMsg()).toBe('authorization=[redacted]');
      });

      it('redacts an axios-config-shaped object', () => {
        console.error({
          url: '/x',
          headers: { Accept: 'json', Authorization: 'Basic zzzzzz' },
          data: { password: 'hunter2' },
        });
        const msg = lastMsg();
        expect(msg).not.toMatch(/zzzzzz|hunter2/);
        expect(msg).toMatch(/"Authorization":"\[redacted\]"/);
        expect(msg).toMatch(/"password":"\[redacted\]"/);
      });

      it('redacts a Digest header to the end of the line', () => {
        console.warn('Authorization: Digest username="x", response="y"');
        expect(lastMsg()).toBe('Authorization: [redacted]');
      });

      it('redacts a Digest value inside JSON, escaped quotes included', () => {
        console.warn({ Authorization: 'Digest username="x", response="y"' });
        const msg = lastMsg();
        expect(msg).toBe('{"Authorization":"[redacted]"}');
      });

      it('redacts a quoted Bearer value and "bearer:"', () => {
        console.warn('a Bearer "abcdefgh12345" b');
        expect(lastMsg()).toBe('a Bearer [redacted] b');
        console.warn('bearer: abcdefgh12345 c');
        expect(lastMsg()).toBe('bearer [redacted] c');
      });

      it('keeps a 7-char Bearer value but redacts 8 chars', () => {
        console.warn('Bearer abcdefg');
        expect(lastMsg()).toBe('Bearer abcdefg');
        console.warn('Bearer abcdefgh');
        expect(lastMsg()).toBe('Bearer [redacted]');
      });
    });

    describe('unquoted Authorization values, any scheme', () => {
      it.each(['Custom abcdefghijklmnop', 'Bearer abcdefghijklmnop', 'Negotiate YIIabc'])(
        'redacts %s to the end of the line',
        (value) => {
          console.warn(`Authorization: ${value}`);
          expect(lastMsg()).toBe('Authorization: [redacted]');
        }
      );

      it('redacts an AWS4-HMAC-SHA256 header completely', () => {
        const cred = ['AKIA', 'EXAMPLE'].join('');
        console.warn(
          `Authorization: AWS4-HMAC-SHA256 Credential=${cred}/20260101/us-east-1/s3/aws4_request, ` +
            'SignedHeaders=host;x-amz-date, Signature=deadbeef01'
        );
        const msg = lastMsg();
        expect(msg).toBe('Authorization: [redacted]');
      });

      it('redacts a Negotiate token built at runtime', () => {
        const blob = Buffer.from('negotiate-ticket-bytes-0123456789').toString('base64');
        console.warn(`Authorization: Negotiate ${blob}`);
        expect(lastMsg()).toBe('Authorization: [redacted]');
      });

      it('redacts Proxy-Authorization too', () => {
        console.warn('Proxy-Authorization: Custom abc123');
        expect(lastMsg()).toBe('Proxy-Authorization: [redacted]');
      });

      it('keeps the next line of multi-line text', () => {
        console.warn('Authorization: Custom abc\nnext line');
        const logs = SessionLogger.getLogs();
        expect(logs.endsWith('WARN Authorization: [redacted]\nnext line')).toBe(true);
      });

      it('stops at & in a URL query', () => {
        console.warn('?Authorization=Custom%20abc&x=1');
        expect(lastMsg()).toBe('?Authorization=[redacted]&x=1');
      });

      it('still replaces quoted and JSON values inside their quotes', () => {
        console.warn({ Authorization: 'Custom abcdefghijklmnop', other: 1 });
        expect(lastMsg()).toBe('{"Authorization":"[redacted]","other":1}');
      });
    });

    describe('JWT boundaries and fragments', () => {
      const jwt = (): string => fakeJwt('{"sub":"PAYLOADX"}', 'signaturesig');

      it('redacts a JWT after - and _', () => {
        console.warn(`jwt-${jwt()} auth_${jwt()}`);
        expect(lastMsg()).toBe('jwt-[jwt] auth_[jwt]');
      });

      it('does not treat eyJ inside a longer alphanumeric run as a JWT start', () => {
        console.warn('abceyJ.x.y');
        expect(lastMsg()).toBe('abceyJ.x.y');
      });

      it('redacts two glued JWTs', () => {
        console.warn(jwt() + jwt());
        expect(lastMsg()).toBe('[jwt][jwt]');
      });

      it('redacts a dotless eyJ fragment', () => {
        console.warn('frag ' + Buffer.from('{"alg":"HS256"}').toString('base64url') + ' end');
        expect(lastMsg()).not.toMatch(/eyJ/);
      });
    });

    describe('email local part', () => {
      it.each([
        ["o'brien@example.com", '[email]'],
        ['jane/doe@x.com', '[email]'],
        ['first!last#x$y@x.com', '[email]'],
        ['a=jane@x.com', 'a=[email]'],
        ['?email=jane@x.com&b=1', '?email=[email]&b=1'],
        ['a@b.c.', '[email].'],
        ['user@localhost', 'user@localhost'],
      ])('%s -> %s', (input, expected) => {
        console.warn(input);
        expect(lastMsg()).toBe(expected);
      });
    });

    describe('budgeted serialisation', () => {
      it('stops walking a 300k-key object once the budget is spent', () => {
        const obj: Record<string, number> = {};
        for (let i = 0; i < 300000; i++) obj['k' + i] = i;
        // The engine itself needs ~100 ms just to enumerate 300k keys (V8
        // builds the key list up front, for-in and Object.keys alike), so a
        // fixed 50 ms ceiling cannot hold. The serialiser must cost no more
        // than that enumeration, i.e. it must not walk or stringify the values.
        let t = performance.now();
        Object.keys(obj);
        const enumerate = performance.now() - t;
        t = performance.now();
        console.warn(obj);
        const logged = performance.now() - t;
        expect(logged).toBeLessThan(enumerate * 3 + 25);
        const msg = lastMsg();
        expect(msg.length).toBeLessThanOrEqual(1000 + '…[truncated]'.length);
        expect(msg.startsWith('{"k0":0,"k1":1')).toBe(true);
      });

      it('still reports circular and BigInt-containing objects as unserializable', () => {
        const c: Record<string, unknown> = {};
        c.self = c;
        console.warn(c);
        expect(lastMsg()).toBe('[unserializable]');
        console.warn({ n: BigInt(1) });
        expect(lastMsg()).toBe('[unserializable]');
      });

      it('matches JSON.stringify for ordinary values', () => {
        const v = { a: [1, 'x', null, undefined, () => 1], b: undefined, c: new Date(0), d: NaN };
        console.warn(v);
        expect(lastMsg()).toBe(JSON.stringify(v));
      });
    });

    describe('exact 100 KB boundary', () => {
      // "[<24-char ISO ts>] WARN " is 32 chars, so a line is 32 + msg length.
      const fill = (firstLen: number): void => {
        console.warn('F' + 'y'.repeat(firstLen - 1));
        for (let i = 0; i < 99; i++) console.warn('y'.repeat(991));
      };

      it('keeps every line when the total is exactly 102,400 bytes', () => {
        fill(992);
        const logs = SessionLogger.getLogs();
        expect(Buffer.byteLength(logs, 'utf8')).toBe(102400);
        expect(logs).toMatch(/WARN Fy/);
      });

      it('drops the oldest line when the total would be 102,401 bytes', () => {
        fill(993);
        const logs = SessionLogger.getLogs();
        expect(Buffer.byteLength(logs, 'utf8')).toBeLessThanOrEqual(102400);
        expect(logs).not.toMatch(/WARN Fy/);
        expect(logs.split('\n')).toHaveLength(99);
      });
    });

    describe('clip bookkeeping mutation-killers', () => {
      it('strips a user-supplied internal clip marker', () => {
        console.warn('a' + '\uE000' + 'b');
        expect(lastMsg()).toBe('ab');
      });

      it('adds the marker when a redaction swallows the clip point', () => {
        console.warn('password="' + 'a'.repeat(5000));
        const msg = lastMsg();
        expect(msg.endsWith('…[truncated]')).toBe(true);
        expect(msg).not.toMatch(/aaaa/);
      });

      it('stops serialising arguments once the work size is reached', () => {
        let touched = 0;
        const arg = (): object => ({
          toJSON(): string {
            touched++;
            return 'x'.repeat(1000);
          },
        });
        const start = performance.now();
        console.warn(...Array.from({ length: 50 }, arg));
        expect(performance.now() - start).toBeLessThan(50);
        // 1,001 chars per argument: the loop must stop after about 4-5 of 50.
        expect(touched).toBeLessThan(10);
        expect(lastMsg().length).toBeLessThanOrEqual(1000 + '…[truncated]'.length);
      });
    });

    describe('linearity of redact()', () => {
      const build = (unit: string, n: number): string =>
        unit.repeat(Math.ceil(n / unit.length)).slice(0, n);
      const perCall = (input: string, reps: number): number => {
        redact(input); // warm up
        const times: number[] = [];
        for (let i = 0; i < 5; i++) {
          const start = performance.now();
          for (let r = 0; r < reps; r++) redact(input);
          times.push((performance.now() - start) / reps);
        }
        return times.sort((x, y) => x - y)[2]; // median
      };
      const units = [
        'a',
        'a.',
        'a_',
        'a.@',
        'a@',
        'a=',
        'eyJ',
        'eyJ.',
        'eyJ-',
        'eyJ.a.',
        'token',
        'a_token',
        'token=',
        'password="',
        "secret: '",
        'Authorization: ',
        'Authorization: Digest ',
        'Authorization: x ',
        'Authorization: a,',
        'Proxy-Authorization:\n',
        'Bearer ',
        'bearer: x',
        'Basic ',
        '\\"',
        'Authorization: token=a@b.c eyJ.a.b Bearer x_',
      ];

      it.each(units)('is linear on %j (16,000 vs 4,000 chars)', (unit) => {
        const t4 = perCall(build(unit, 4000), 10);
        const t16 = perCall(build(unit, 16000), 10);
        // Linear: ~4x. Quadratic: ~16x (limit 12 leaves room for timer noise). The floor keeps timer noise on very
        // fast inputs from inflating the ratio.
        expect(t16 / Math.max(t4, 0.25)).toBeLessThan(12);
        expect(t16).toBeLessThan(50);
      });
    });
  });
});
