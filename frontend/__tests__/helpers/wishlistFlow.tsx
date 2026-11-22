import { act, renderHook, waitFor } from '@testing-library/react-native';

import { useBanner } from '../../hooks/useBanner';
import { useScanJobs } from '../../hooks/useScanJobs';
import type { EnrichedBook } from '../../components/BookCandidatePicker';
import { ScanJobsWrapper } from './providers';
import { scanMocks } from './scanJobMocks';

/** A scanned book as the review sheet hands it to `handleAddBooks`. */
export const book = (title: string): EnrichedBook => ({
  open_library_work_id: `OL-${title}`,
  title,
  author: 'Frank Herbert',
  subjects: [],
  confidence: 0.9,
  already_in_library: false,
  in_library: false,
  ownership: 'not_owned',
  editions: [{ isbn_13: '9780441013593' }],
  catalog_token: 'p.mac',
});

/** An axios-style HTTP error carrying `{ detail }`. */
export function httpError(status: number, detail: unknown) {
  return Object.assign(new Error(`Request failed with status code ${status}`), {
    response: { status, data: { detail } },
  });
}

/**
 * Scan one photo that finds `titles`, open the review, then add them all:
 * every POST /wishlist is answered by `answer(title)`. Returns the hook so a
 * test can read the scan state and the banner.
 */
export async function scanAndAdd(titles: string[], answer: (title: string) => Promise<unknown>) {
  const mockPost = scanMocks.post;
  const hook = await renderHook(() => ({ scan: useScanJobs(), banner: useBanner() }), {
    wrapper: ScanJobsWrapper,
  });
  await waitFor(() => expect(hook.result.current.scan.jobs).toEqual([]));
  mockPost.mockResolvedValueOnce({
    data: { books: titles.map(book), enhanced: false, enhanced_scans_remaining: null },
  });
  await act(async () => {
    await hook.result.current.scan.startScan('image', 'file:///a.jpg');
  });
  await act(() => {
    hook.result.current.scan.reviewJob(hook.result.current.scan.jobs[0].id);
  });
  mockPost.mockImplementation((_url: string, b: { title: string }) => answer(b.title));
  await act(async () => {
    await hook.result.current.scan.handleAddBooks(titles.map(book));
  });
  return hook;
}
