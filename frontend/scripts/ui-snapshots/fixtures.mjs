// Mock API data for the UI snapshot run. Titles are public-domain classics so
// screenshots carry no real user data. Covers are generated SVGs served by the
// mock (see coverSvg), so the run needs no network beyond the dev server.

const BOOKS = [
  ['Pride and Prejudice', 'Jane Austen', 1813, 'read', '#7a3b2e'],
  ['Moby-Dick', 'Herman Melville', 1851, 'reading', '#1f3b57'],
  ['Middlemarch', 'George Eliot', 1871, 'purchased', '#4d5b3a'],
  ['The Count of Monte Cristo', 'Alexandre Dumas', 1844, 'wishlisted', '#5a2d4f'],
  ['Great Expectations', 'Charles Dickens', 1861, 'read', '#8a6a2b'],
  ['Anna Karenina', 'Leo Tolstoy', 1878, 'wishlisted', '#2e4f4a'],
  ['Frankenstein; or, The Modern Prometheus', 'Mary Shelley', 1818, 'reading', '#333a44'],
  ['Wuthering Heights', 'Emily Brontë', 1847, 'wishlisted', '#6b4a3a'],
  ['The Picture of Dorian Gray', 'Oscar Wilde', 1890, 'purchased', '#3e3466'],
  ['Little Women', 'Louisa May Alcott', 1868, 'read', '#7d4b5c'],
];

export function coverSvg(i) {
  const [title, author, , , color] = BOOKS[i % BOOKS.length];
  const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;');
  return `<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450" viewBox="0 0 300 450">
  <rect width="300" height="450" fill="${color}"/>
  <rect x="18" y="18" width="264" height="414" fill="none" stroke="#e9dcc0" stroke-width="2" opacity="0.6"/>
  <foreignObject x="30" y="90" width="240" height="220">
    <div xmlns="http://www.w3.org/1999/xhtml" style="font-family:Georgia,serif;color:#f4ead5;font-size:30px;line-height:1.15;text-align:center">${esc(title)}</div>
  </foreignObject>
  <text x="150" y="380" fill="#e9dcc0" font-family="Georgia,serif" font-size="18" text-anchor="middle">${esc(author)}</text>
</svg>`;
}

export function userBooks(base, { withoutCover = [2, 7] } = {}) {
  return BOOKS.map(([title, author, year, status], i) => ({
    id: `ub-${i}`,
    status,
    rating: status === 'read' ? 4 : null,
    notes: null,
    unverified: i === 9,
    book: {
      id: `b-${i}`,
      title,
      author,
      cover_url: withoutCover.includes(i) ? null : `${base}/covers/${i}.svg`,
      description:
        'A classic of the English-language canon, reissued in a handsome edition with an introduction and notes.',
    },
    edition: { publish_year: year },
  }));
}

export function trash(base) {
  return userBooks(base)
    .slice(0, 3)
    .map((b, i) => ({
      ...b,
      deleted_at: new Date(Date.now() - (i + 1) * 86400000).toISOString(),
      purge_after: new Date(Date.now() + (29 - i) * 86400000).toISOString(),
    }));
}

export function searchResults(base) {
  return BOOKS.slice(0, 4).map(([title, author, year], i) => ({
    book_id: `b-${i}`,
    // The last result has no catalog id, so the "Add it yourself" row shows.
    google_books_id: i === 3 ? undefined : `gb-${i}`,
    title,
    author,
    description: 'A classic novel.',
    cover_url: i === 3 ? undefined : `${base}/covers/${i}.svg`,
    subjects: ['Fiction', 'Classics'],
    confidence: 0.95 - i * 0.1,
    already_in_library: i === 0,
    in_library: i === 0,
    ownership: i === 0 ? 'owned_same_edition' : 'not_owned',
    editions: [{ isbn_13: `97800000000${i}0`, publisher: 'Penguin Classics', publish_year: year }],
  }));
}

export const usage = {
  used_today: 3,
  daily_limit: 20,
  found_used_today: 3,
  found_daily_limit: 20,
  no_match_used_today: 1,
  no_match_daily_limit: 10,
};
