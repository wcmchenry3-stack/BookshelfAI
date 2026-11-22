#!/usr/bin/env node
/* eslint-env node */
/**
 * Translate English locale files to all supported languages using Claude.
 *
 * Dev-only tool: it runs on a developer's machine and calls the Anthropic
 * Messages API directly over HTTPS (no SDK, no app code). It is never part of
 * the app or CI. Model calls made by the app itself go through the backend AI
 * gateway (backend/app/ai/gateway.py), not through here.
 *
 * Usage:
 *   node scripts/translate.js --locale es --namespace scan
 *   node scripts/translate.js --locale es --namespace scan --force    # retranslate all keys
 *   node scripts/translate.js --locale es --namespace scan --dry-run  # preview only
 *   node scripts/translate.js --all                                   # all locales × namespaces
 *
 * Requires ANTHROPIC_API_KEY in env. Uses the pinned model below
 * (claude-sonnet-5-5; no sampling parameters: this model rejects them).
 */

const fs = require('fs');
const path = require('path');
const https = require('https');

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

const LOCALES_DIR = path.join(__dirname, '../src/i18n/locales');
const NAMESPACES = [
  'common',
  'auth',
  'tabs',
  'my-books',
  'scan',
  'settings',
  'wishlist',
  'components',
];
const NON_ENGLISH_LOCALES = [
  'fr-CA',
  'es',
  'hi',
  'ar',
  'zh',
  'ja',
  'ko',
  'pt',
  'he',
  'de',
  'nl',
  'ru',
];
const BATCH_SIZE = 20;
const NEEDS_TRANSLATION = '__NEEDS_TRANSLATION__';

// Load glossary (do-not-translate terms)
let glossaryTerms = [];
try {
  // Glossary is TypeScript — extract the term strings with a regex
  const glossaryPath = path.join(__dirname, '../src/i18n/glossary.ts');
  const src = fs.readFileSync(glossaryPath, 'utf8');
  const matches = [...src.matchAll(/term:\s*'([^']+)'/g)];
  glossaryTerms = matches.map((m) => m[1]);
} catch {
  console.warn('Could not load glossary — no terms will be protected');
}

// ---------------------------------------------------------------------------
// CLI args
// ---------------------------------------------------------------------------

const args = process.argv.slice(2);
const getFlag = (flag) => {
  const i = args.indexOf(flag);
  return i !== -1 ? args[i + 1] : null;
};
const hasFlag = (flag) => args.includes(flag);

const targetLocale = getFlag('--locale');
const targetNamespace = getFlag('--namespace');
const dryRun = hasFlag('--dry-run');
const force = hasFlag('--force');
const runAll = hasFlag('--all');

if (!runAll && (!targetLocale || !targetNamespace)) {
  console.error('Usage: node scripts/translate.js --locale <code> --namespace <ns>');
  console.error('       node scripts/translate.js --all');
  process.exit(1);
}

if (!process.env.ANTHROPIC_API_KEY) {
  console.error('Error: ANTHROPIC_API_KEY is not set');
  process.exit(1);
}

// ---------------------------------------------------------------------------
// Claude helpers
// ---------------------------------------------------------------------------

const MODEL = 'claude-sonnet-5-5';
// Room for 20 translated strings plus the model's own reasoning.
const MAX_TOKENS = 16000;

function claudeRequest(body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const options = {
      hostname: 'api.anthropic.com',
      path: '/v1/messages',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': process.env.ANTHROPIC_API_KEY,
        'anthropic-version': '2023-06-01',
        'Content-Length': Buffer.byteLength(data),
      },
    };

    const req = https.request(options, (res) => {
      let body = '';
      res.on('data', (chunk) => (body += chunk));
      res.on('end', () => {
        try {
          resolve(JSON.parse(body));
        } catch (_e) {
          reject(new Error(`JSON parse error (HTTP ${res.statusCode})`));
        }
      });
    });

    req.on('error', reject);
    req.write(data);
    req.end();
  });
}

async function translateBatch(entries, targetLang, namespace, meta) {
  const keyValuePairs = Object.fromEntries(entries);

  const metaContext = entries
    .map(([k]) => {
      const flat = k.replace(/\./g, '.');
      const m = meta[flat] || meta[k];
      if (!m) return null;
      const parts = [`Key "${k}": ${m.description}`];
      if (m.tone) parts.push(`tone: ${m.tone}`);
      if (m.characterLimit) parts.push(`max ${m.characterLimit} chars`);
      if (m.placeholders) parts.push(`preserve placeholders: ${m.placeholders.join(', ')}`);
      if (m.notes) parts.push(`note: ${m.notes}`);
      return parts.join('; ');
    })
    .filter(Boolean)
    .join('\n');

  const glossaryInstructions =
    glossaryTerms.length > 0
      ? `\n\nDo NOT translate these terms — keep them exactly as-is: ${glossaryTerms.join(', ')}`
      : '';

  const prompt = `You are a professional mobile app translator. Translate the following JSON key-value pairs from English to ${targetLang}.

Rules:
- Return ONLY valid JSON with the same keys
- Preserve all {{placeholder}} tokens exactly as-is
- Preserve all \\n newline sequences
- Keep translated values natural and friendly for a mobile reading app
- Keep translations concise — mobile UI has limited space${glossaryInstructions}

Per-key context:
${metaContext || 'No additional context'}

English source:
${JSON.stringify(keyValuePairs, null, 2)}`;

  const response = await claudeRequest({
    model: MODEL,
    max_tokens: MAX_TOKENS,
    // Translation needs little reasoning; keep the calls quick and cheap.
    output_config: { effort: 'low' },
    messages: [{ role: 'user', content: prompt }],
  });

  if (response.type === 'error' || response.error) {
    throw new Error(`Claude error: ${JSON.stringify(response.error)}`);
  }
  if (response.stop_reason === 'refusal') throw new Error('Claude declined the request');
  if (response.stop_reason === 'max_tokens') throw new Error('Claude reply was cut off');

  const content = (response.content || [])
    .filter((block) => block.type === 'text')
    .map((block) => block.text)
    .join('');
  if (!content) throw new Error('Empty response from Claude');

  return JSON.parse(extractJson(content));
}

// Claude has no JSON mode here, so accept a bare object or one wrapped in a
// code fence or a sentence: take the text from the first "{" to the last "}".
function extractJson(text) {
  const start = text.indexOf('{');
  const end = text.lastIndexOf('}');
  if (start === -1 || end < start) throw new Error('No JSON object in Claude reply');
  return text.slice(start, end + 1);
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

function validateTranslation(original, translated, keys) {
  const errors = [];

  for (const key of keys) {
    const orig = original[key];
    const trans = translated[key];

    if (trans === undefined || trans === null) {
      errors.push(`Missing key: ${key}`);
      continue;
    }

    // Check placeholders are preserved
    const placeholders = [...String(orig).matchAll(/\{\{[^}]+\}\}/g)].map((m) => m[0]);
    for (const ph of placeholders) {
      if (!String(trans).includes(ph)) {
        errors.push(`Missing placeholder ${ph} in key "${key}"`);
      }
    }

    // Check glossary terms are not translated
    for (const term of glossaryTerms) {
      if (String(orig).includes(term) && !String(trans).includes(term)) {
        errors.push(`Glossary term "${term}" was translated in key "${key}"`);
      }
    }
  }

  return errors;
}

// ---------------------------------------------------------------------------
// Flatten / unflatten nested JSON
// ---------------------------------------------------------------------------

function flattenObject(obj, prefix = '') {
  const result = {};
  for (const [k, v] of Object.entries(obj)) {
    const fullKey = prefix ? `${prefix}.${k}` : k;
    if (typeof v === 'object' && v !== null && !Array.isArray(v)) {
      Object.assign(result, flattenObject(v, fullKey));
    } else {
      result[fullKey] = v;
    }
  }
  return result;
}

function unflattenObject(flat) {
  const result = {};
  for (const [key, value] of Object.entries(flat)) {
    const parts = key.split('.');
    let current = result;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!current[parts[i]]) current[parts[i]] = {};
      current = current[parts[i]];
    }
    current[parts[parts.length - 1]] = value;
  }
  return result;
}

// ---------------------------------------------------------------------------
// Main translation function
// ---------------------------------------------------------------------------

async function translateNamespace(locale, namespace) {
  const langNames = {
    'fr-CA': 'Canadian French',
    es: 'Spanish',
    hi: 'Hindi',
    ar: 'Arabic',
    zh: 'Simplified Chinese',
    ja: 'Japanese',
    ko: 'Korean',
    pt: 'Brazilian Portuguese',
    he: 'Hebrew',
    de: 'German',
    nl: 'Dutch',
    ru: 'Russian',
  };

  const langName = langNames[locale] || locale;
  const enPath = path.join(LOCALES_DIR, 'en', `${namespace}.json`);
  const outPath = path.join(LOCALES_DIR, locale, `${namespace}.json`);
  const metaPath = path.join(LOCALES_DIR, '_meta', `${namespace}.meta.json`);

  if (!fs.existsSync(enPath)) {
    console.error(`  Source not found: ${enPath}`);
    return;
  }

  const enSource = JSON.parse(fs.readFileSync(enPath, 'utf8'));
  const meta = fs.existsSync(metaPath) ? JSON.parse(fs.readFileSync(metaPath, 'utf8')) : {};

  // Load existing translations (if any)
  let existing = {};
  if (fs.existsSync(outPath)) {
    try {
      existing = JSON.parse(fs.readFileSync(outPath, 'utf8'));
    } catch {
      existing = {};
    }
  }

  const flatSource = flattenObject(enSource);
  const flatExisting = flattenObject(existing);

  // Determine which keys need translation
  const keysToTranslate = Object.keys(flatSource).filter((k) => {
    if (force) return true;
    const current = flatExisting[k];
    return !current || current === NEEDS_TRANSLATION;
  });

  if (keysToTranslate.length === 0) {
    console.log(
      `  [${locale}/${namespace}] All keys already translated — skip (use --force to redo)`
    );
    return;
  }

  console.log(
    `  [${locale}/${namespace}] Translating ${keysToTranslate.length} key(s) to ${langName}…`
  );

  if (dryRun) {
    console.log(`  [dry-run] Would translate: ${keysToTranslate.join(', ')}`);
    return;
  }

  // Process in batches
  const result = { ...flatExisting };

  for (let i = 0; i < keysToTranslate.length; i += BATCH_SIZE) {
    const batch = keysToTranslate.slice(i, i + BATCH_SIZE);
    const batchEntries = batch.map((k) => [k, flatSource[k]]);

    try {
      const translated = await translateBatch(batchEntries, langName, namespace, meta);
      const errors = validateTranslation(flatSource, translated, batch);

      if (errors.length > 0) {
        console.warn(`  [${locale}/${namespace}] Validation warnings:`, errors);
      }

      for (const key of batch) {
        result[key] = translated[key] ?? NEEDS_TRANSLATION;
      }
    } catch (err) {
      console.error(`  [${locale}/${namespace}] Batch failed:`, err.message);
      for (const key of batch) {
        result[key] = NEEDS_TRANSLATION;
      }
    }
  }

  const output = unflattenObject(result);

  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(output, null, 2) + '\n', 'utf8');
  console.log(`  [${locale}/${namespace}] Written → ${outPath}`);
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

async function main() {
  const jobs = runAll
    ? NON_ENGLISH_LOCALES.flatMap((l) => NAMESPACES.map((ns) => ({ locale: l, namespace: ns })))
    : [{ locale: targetLocale, namespace: targetNamespace }];

  console.log(
    `Translate script — ${jobs.length} job(s)${dryRun ? ' [dry-run]' : ''}${force ? ' [force]' : ''}`
  );

  for (const { locale, namespace } of jobs) {
    await translateNamespace(locale, namespace);
  }

  console.log('Done.');
}

main().catch((err) => {
  console.error('Fatal:', err);
  process.exit(1);
});
