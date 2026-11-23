import { createSwipeReleaseDecider } from '../../components/library/swipeRelease';

const FULL = 176;

describe('createSwipeReleaseDecider (#935)', () => {
  it('settles a full swipe on whichever event arrives second', () => {
    const a = createSwipeReleaseDecider(FULL);
    a.begin();
    expect(a.release(180)).toBe(false);
    expect(a.outcome('open')).toBe(true);

    const b = createSwipeReleaseDecider(FULL);
    b.begin();
    expect(b.outcome('open')).toBe(false);
    expect(b.release(180)).toBe(true);
  });

  it('counts exactly the distance as full, and anything under it as not', () => {
    const d = createSwipeReleaseDecider(FULL);
    d.release(FULL);
    expect(d.outcome('open')).toBe(true);
    d.release(FULL - 1);
    expect(d.outcome('open')).toBe(false);
  });

  it('never removes when the swipe closes the row', () => {
    const d = createSwipeReleaseDecider(FULL);
    d.release(400);
    expect(d.outcome('close')).toBe(false);
  });

  it('settles once: a second outcome does not reuse the release', () => {
    const d = createSwipeReleaseDecider(FULL);
    d.release(200);
    expect(d.outcome('open')).toBe(true);
    expect(d.outcome('open')).toBe(false);
  });

  it('begin drops an unpaired release or outcome from the previous touch', () => {
    const d = createSwipeReleaseDecider(FULL);
    d.release(200);
    d.begin();
    expect(d.outcome('open')).toBe(false);

    d.begin();
    d.outcome('open');
    d.begin();
    expect(d.release(200)).toBe(false);
  });
});
