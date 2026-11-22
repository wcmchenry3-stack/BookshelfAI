import { catalogRefusal } from '../../lib/ownership';
import { resetScanMocks, scanMocks } from '../helpers/scanJobMocks';
import { httpError, scanAndAdd } from '../helpers/wishlistFlow';

// #612: POST /wishlist answers 422 book_not_in_catalog (no catalog match) or
// 422 catalog_token_invalid (stale result). Both are final: a specific
// message, no retry, no Sentry event.

jest.mock('../../hooks/useAuth', () => require('../helpers/scanJobMocks').authedModule());
jest.mock('../../lib/api', () => require('../helpers/scanJobMocks').scanApiModule());
jest.mock('../../lib/scanJobStorage', () => require('../helpers/scanJobMocks').scanStorageModule());
jest.mock('@react-native-community/netinfo', () =>
  require('../helpers/scanJobMocks').netInfoModule()
);
jest.mock('../../lib/sentry', () => require('../helpers/scanJobMocks').sentryModule());
jest.mock('expo-crypto', () => require('../helpers/scanJobMocks').cryptoModule());

const { captureException: mockCaptureException } = scanMocks;

beforeEach(() => {
  jest.resetAllMocks();
  resetScanMocks();
});

describe('POST /wishlist 422 catalog refusals (#612)', () => {
  it('book_not_in_catalog: specific message, no retry, no Sentry', async () => {
    const hook = await scanAndAdd(['Dune'], () =>
      Promise.reject(httpError(422, 'book_not_in_catalog'))
    );
    expect(hook.result.current.banner.banner).toEqual(
      expect.objectContaining({
        message: "We couldn't find this book in our catalog. Search by title, or add it yourself.",
        type: 'error',
      })
    );
    expect(hook.result.current.scan.reviewingJob).toBeNull();
    expect(hook.result.current.scan.jobs).toHaveLength(0);
    expect(mockCaptureException).not.toHaveBeenCalled();
  });

  it('several books all not in the catalog', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma'], () =>
      Promise.reject(httpError(422, 'book_not_in_catalog'))
    );
    expect(hook.result.current.banner.banner?.message).toBe(
      "We couldn't find these 2 books in our catalog. Search by title, or add them yourself."
    );
    expect(hook.result.current.scan.reviewingJob).toBeNull();
  });

  it('catalog_token_invalid: specific message, no retry, no Sentry', async () => {
    const hook = await scanAndAdd(['Dune'], () =>
      Promise.reject(httpError(422, 'catalog_token_invalid'))
    );
    expect(hook.result.current.banner.banner).toEqual(
      expect.objectContaining({
        message: 'This result is out of date. Scan or search for the book again.',
        type: 'error',
      })
    );
    expect(hook.result.current.scan.reviewingJob).toBeNull();
    expect(hook.result.current.scan.jobs).toHaveLength(0);
    expect(mockCaptureException).not.toHaveBeenCalled();
  });

  it('several stale results', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma'], () =>
      Promise.reject(httpError(422, 'catalog_token_invalid'))
    );
    expect(hook.result.current.banner.banner?.message).toBe(
      'These 2 results are out of date. Scan or search for the books again.'
    );
  });

  it('mixed: one added, one not in catalog, one 500 keeps the 500 and the not-in-catalog book', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma', 'Ulysses'], (title) =>
      title === 'Dune'
        ? Promise.resolve({ data: {} })
        : title === 'Emma'
          ? Promise.reject(httpError(422, 'book_not_in_catalog'))
          : Promise.reject(httpError(500, 'boom'))
    );
    expect(hook.result.current.banner.banner).toEqual(
      expect.objectContaining({
        message:
          'Added 1, 2 not saved. 1 not in our catalog: try searching by title. Tap Add to retry the others.',
        type: 'error',
      })
    );
    // The not-in-catalog book stays in the open picker so its "Add it yourself"
    // button is still reachable (#616); the added book is gone.
    expect(hook.result.current.scan.reviewingJob?.results?.map((b) => b.title)).toEqual([
      'Emma',
      'Ulysses',
    ]);
    // Only the 500 is reported.
    // #724: the central API reporter (lib/api.ts, mocked here) owns the 500.
    expect(mockCaptureException).not.toHaveBeenCalled();
  });

  it('mixed: added next to a stale result needs no retry hint and closes the review', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma'], (title) =>
      title === 'Dune'
        ? Promise.resolve({ data: {} })
        : Promise.reject(httpError(422, 'catalog_token_invalid'))
    );
    expect(hook.result.current.banner.banner?.message).toBe(
      'Added 1, 1 not saved. 1 out of date: scan or search again.'
    );
    expect(hook.result.current.scan.reviewingJob).toBeNull();
    expect(mockCaptureException).not.toHaveBeenCalled();
  });

  const own = () => Promise.reject(httpError(409, 'owned_same_edition'));
  const nic = () => Promise.reject(httpError(422, 'book_not_in_catalog'));
  const stale = () => Promise.reject(httpError(422, 'catalog_token_invalid'));
  const boom = () => Promise.reject(httpError(500, 'boom'));
  const ok = () => Promise.resolve({ data: {} });
  const pick = (m: Record<string, () => Promise<unknown>>) => (t: string) => m[t]();

  it('mixed: added, owned and not in catalog', async () => {
    const hook = await scanAndAdd(
      ['Dune', 'Emma', 'Ulysses'],
      pick({ Dune: ok, Emma: own, Ulysses: nic })
    );
    expect(hook.result.current.banner.banner?.message).toBe(
      'Added 1, 1 already in your library, 1 not saved. 1 not in our catalog: try searching by title.'
    );
    expect(hook.result.current.scan.reviewingJob).toBeNull();
    expect(mockCaptureException).not.toHaveBeenCalled();
  });

  it('mixed: owned and not in catalog, nothing added, has no "Added 0"', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma'], pick({ Dune: own, Emma: nic }));
    expect(hook.result.current.banner.banner?.message).toBe(
      '1 already in your library, 1 not saved. 1 not in our catalog: try searching by title.'
    );
  });

  it('both refusal kinds, nothing added', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma'], pick({ Dune: nic, Emma: stale }));
    expect(hook.result.current.banner.banner?.message).toBe(
      '2 not saved. 1 not in our catalog: try searching by title. 1 out of date: scan or search again.'
    );
    expect(hook.result.current.scan.reviewingJob).toBeNull();
  });

  it('refused plus a 500, nothing added', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma'], pick({ Dune: nic, Emma: boom }));
    expect(hook.result.current.banner.banner?.message).toBe(
      '2 not saved. 1 not in our catalog: try searching by title. Tap Add to retry the others.'
    );
    // Both stay in the open picker: the 500 to retry, the refused book for
    // "Add it yourself" (#616).
    expect(hook.result.current.scan.reviewingJob?.results?.map((b) => b.title)).toEqual([
      'Dune',
      'Emma',
    ]);
  });

  it('refused, owned and 500 together keep the 500 and the refused book', async () => {
    const hook = await scanAndAdd(
      ['Dune', 'Emma', 'Ulysses'],
      pick({ Dune: nic, Emma: own, Ulysses: boom })
    );
    expect(hook.result.current.banner.banner?.message).toBe(
      '1 already in your library, 2 not saved. 1 not in our catalog: try searching by title. Tap Add to retry the others.'
    );
    expect(hook.result.current.scan.reviewingJob?.results?.map((b) => b.title)).toEqual([
      'Dune',
      'Ulysses',
    ]);
    // #724: the central API reporter (lib/api.ts, mocked here) owns the 500.
    expect(mockCaptureException).not.toHaveBeenCalled();
  });

  it('shows the whole banner, not two lines', async () => {
    const hook = await scanAndAdd(['Dune', 'Emma'], pick({ Dune: nic, Emma: stale }));
    expect(hook.result.current.banner.banner?.numberOfLines).toBe(0);
  });

  it('a 422 with another detail is still a retryable failure', async () => {
    const hook = await scanAndAdd(['Dune'], () => Promise.reject(httpError(422, 'other')));
    expect(hook.result.current.banner.banner?.message).toBe(
      'Failed to add book to wishlist. Please try again.'
    );
    expect(hook.result.current.scan.reviewingJob).not.toBeNull();
    expect(mockCaptureException).toHaveBeenCalledTimes(1);
  });
});

describe('catalogRefusal', () => {
  it('recognises the two 422 codes only', () => {
    expect(catalogRefusal(httpError(422, 'book_not_in_catalog'))).toBe('book_not_in_catalog');
    expect(catalogRefusal(httpError(422, 'catalog_token_invalid'))).toBe('catalog_token_invalid');
    expect(catalogRefusal(httpError(409, 'book_not_in_catalog'))).toBeNull();
    expect(catalogRefusal(httpError(422, 'x'))).toBeNull();
    expect(catalogRefusal(new Error('network'))).toBeNull();
    expect(catalogRefusal(undefined)).toBeNull();
  });
});
