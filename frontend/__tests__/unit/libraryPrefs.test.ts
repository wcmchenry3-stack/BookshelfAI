import { loadLibrarySort, saveLibrarySort } from '../../lib/libraryPrefs';
import { getItem, setItem } from '../../lib/storage';

jest.mock('../../lib/storage', () => ({ getItem: jest.fn(), setItem: jest.fn() }));

const KEYS = ['added', 'title', 'author'] as const;

beforeEach(() => jest.clearAllMocks());

describe('libraryPrefs (#933)', () => {
  it('saves the sort under library.sort', async () => {
    await saveLibrarySort('title');
    expect(setItem).toHaveBeenCalledWith('library.sort', 'title');
  });

  it('loads a saved sort', async () => {
    (getItem as jest.Mock).mockResolvedValue('author');
    await expect(loadLibrarySort(KEYS)).resolves.toBe('author');
    expect(getItem).toHaveBeenCalledWith('library.sort');
  });

  it('gives null for nothing saved or an unknown value', async () => {
    (getItem as jest.Mock).mockResolvedValueOnce(null);
    await expect(loadLibrarySort(KEYS)).resolves.toBeNull();
    (getItem as jest.Mock).mockResolvedValueOnce('priority');
    await expect(loadLibrarySort(KEYS)).resolves.toBeNull();
  });

  it('never rejects when the store fails', async () => {
    (getItem as jest.Mock).mockRejectedValue(new Error('keychain'));
    (setItem as jest.Mock).mockRejectedValue(new Error('keychain'));
    await expect(loadLibrarySort(KEYS)).resolves.toBeNull();
    await expect(saveLibrarySort('title')).resolves.toBeUndefined();
  });
});
