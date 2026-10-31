/**
 * Terms that must never be translated.
 * Used by scripts/translate.js to validate AI output.
 */
export const glossary = [
  {
    term: 'Bookshelf',
    doNotTranslate: true,
    reason: 'App name — must remain identical in every locale',
    definition: 'The name of this application',
  },
  {
    term: 'Google',
    doNotTranslate: true,
    reason: 'Proper name of the authentication provider',
    definition: 'Google OAuth sign-in service',
  },
  {
    term: 'Open Library',
    doNotTranslate: true,
    reason: 'Proper name of the book data source',
    definition: 'Open Library by the Internet Archive',
  },
  {
    term: 'ISBN',
    doNotTranslate: true,
    reason: 'International standard acronym — identical in all languages',
    definition: 'International Standard Book Number',
  },
  {
    term: 'GPT',
    doNotTranslate: true,
    reason: 'Model name acronym',
    definition: 'Generative Pre-trained Transformer model reference',
  },
];
