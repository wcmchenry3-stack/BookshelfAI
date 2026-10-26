# AI scan evaluation (#536)

A repeatable, on-demand evaluation of one scan model on the owner's real,
labelled shelf photos, used to pick the model per tier (default: Haiku 5.5 for
`scan.standard`, Sonnet 5.5 for `scan.enhanced`, #547). It is **not** a test
gate and never runs in CI's test jobs; see `docs/testing.md` ("AI scan
evaluation") for when to run it and what the report holds.

| File | Role |
|---|---|
| `ai_scan/run_eval.py` | CLI entry point |
| `ai_scan/runner.py` | Spend cap, the adapter call, the report |
| `ai_scan/dataset.py` | R2 location from the environment, manifest validation, temporary downloads |
| `ai_scan/scoring.py` | Deterministic matching and precision / recall |
| `../requirements-eval.txt` | The app requirements plus the pinned S3 client (boto3) |
| `../../.github/workflows/ai-eval.yml` | The on-demand workflow |

## The dataset never enters git

The photos and their ground truth live **only** in a private, encrypted
Cloudflare R2 bucket (#546). Nothing from the dataset is committed, stored in
Git LFS or uploaded as a CI artifact: `.gitignore` ignores image and manifest
files under `backend/eval/`, and `tests/unit/test_ai_eval_repo.py` fails if one
is tracked. The runner downloads each photo into a private temporary directory
(mode 0700, files named `00001.img`, never the object key) that is deleted when
the run ends, also when it fails. The report, the console and the logs never
hold a bucket name, endpoint, object key, case id, title, author, image or
EXIF data (`tests/security/test_ai_eval_privacy.py` plants sentinels in all of
them and checks).

## Dataset layout

One folder in the bucket (the bucket root, or the prefix in
`AI_EVAL_R2_PREFIX`) holding `manifest.json` and the photos:

```
manifest.json
images/case-001.jpg
images/case-002.heic
...
```

`manifest.json`:

```json
{
  "schema_version": 1,
  "dataset_version": "2026-11-01",
  "cases": [
    {
      "id": "case-001",
      "image": "images/case-001.jpg",
      "books": [
        {"title": "Dune", "author": "Frank Herbert", "isbn": "9780441013593"},
        {"title": "Emma", "author": "Jane Austen"}
      ]
    }
  ]
}
```

It is validated strictly before any model call, and the run refuses an invalid
manifest (the error names only field positions and error types):

- `schema_version` is `1`; no unknown keys anywhere.
- `dataset_version`: 1 to 32 of `A-Z a-z 0-9 . _ -`. It is copied into the
  report to identify the dataset revision, so use a date or number, never a
  title.
- `cases`: 1 to 1,000 cases with unique `id`s (1 to 64 of `A-Z a-z 0-9 . _ -`)
  and unique `image` keys.
- `image`: a plain relative object key (`A-Z a-z 0-9 . _ - /`, no leading `/`,
  no `.` or `..` segment) ending in `.jpg`, `.jpeg`, `.png`, `.webp`, `.heic`
  or `.heif`; at most 25 MB.
- `books`: every book you can read in the photo, at most 100 (an empty list is
  allowed: a photo with no readable book tests false positives). List a book
  twice when two copies are on the shelf. `title` and `author` are non-blank
  strings of at most 500 characters. `isbn` is optional: a valid ISBN-10 or
  ISBN-13 (hyphens and spaces allowed, checksum checked).

## How a prediction is scored

Deterministic, no LLM judge (`ai_scan/scoring.py`, rule
`isbn-or-title+author/one-to-one/v1`). A predicted book matches a ground-truth
book when:

1. **ISBN**: both have an ISBN and they are the same ISBN-13 (an ISBN-10 is
   converted with the app's `normalise_isbn_13`), or
2. **title + author**: the titles agree by the app's own rule
   (`app.services.enrichment.titles_agree`: `normalise_title`, so case,
   punctuation, spacing and a leading "The / A / An" are ignored, and a
   subtitle only matters when it is more than an edition note), and the
   authors agree: equal `normalise_text` forms, or the same last word
   (surname: "F. Herbert" matches "Frank Herbert").

Matching is one-to-one: a maximum bipartite matching per photo, so a
prediction finds at most one book and a book is found at most once. A
duplicated prediction is a false positive unless the book is listed twice.
Precision = matched / predicted and recall = matched / ground truth, summed
over every scanned photo (micro); the per-photo means are also reported. A
call that errored, was refused or was malformed predicted nothing, so it
lowers recall (that is what the user would have got).

## Spend cap and cost

Before every call the runner checks that the spend so far plus an upper bound
for one call stays within `--spend-cap-usd`, and stops before the call
otherwise (`spend_cap.stopped_by_cap` in the report). The bound is every
prompt token (4,000 image + 2,000 prompt, more than a normalised photo can
use) at the model's highest input rate plus a full `max_tokens` reply at the
output rate, and never below the registry's `est_cost_ceiling_micros`. An
answered call adds its real cost; an errored call adds the bound (a timeout
may still have been billed). The adapters keep their own retry and backoff on
429 and 5xx, which are not billed.

Cost is priced with the usage ledger's own function
(`app.ai.prices.cost_micros` at `CURRENT_PRICE_VERSION`) from each call's
reported usage. There are no ledger rows: the eval calls the provider adapter
directly (through the same image normalisation as `/scan`), without the app
database, quota, breaker or ledger.

## Owner setup (once)

1. **R2 bucket.** In Cloudflare, create a private R2 bucket for the dataset
   only (no public access, no `r2.dev` URL, no custom domain). R2 encrypts
   objects at rest. Pick a location hint close to you.
2. **Access list.** Only the owner's Cloudflare account members who need it
   have R2 access; nobody else is given the bucket or the token.
3. **Read-only token.** R2 > Manage API tokens > Create API token:
   permission **Object Read only**, applied to **this bucket only**, no IP
   filter needed (GitHub runners). Note the Access Key ID, the Secret Access
   Key and the S3 endpoint `https://<account id>.r2.cloudflarestorage.com`.
   The code cannot check that the token is read-only or bucket-scoped: that is
   up to this step. Rotate it if it ever leaks.
4. **Upload the dataset** with the layout above (the dashboard, `rclone` or
   `aws s3 cp --endpoint-url ...` with a separate write-capable credential
   that is NOT put in GitHub).
5. **GitHub environment.** Repository Settings > Environments > New
   environment `ai-eval`:
   - Deployment branches: **Selected branches**, `dev` only, so a branch with
     modified code cannot run with these secrets.
   - Required reviewers: the owner, so every run is approved.
   - Environment secrets: `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` (keys for
     the eval only, with a low spend limit set at the provider),
     `AI_EVAL_R2_ENDPOINT`, `AI_EVAL_R2_BUCKET`, `AI_EVAL_R2_ACCESS_KEY_ID`,
     `AI_EVAL_R2_SECRET_ACCESS_KEY`. Bucket and endpoint are secrets too, so
     GitHub masks them in any log.
   - Optional environment variable `AI_EVAL_R2_PREFIX` when the dataset is in a
     folder of the bucket.
6. **Run it**: Actions > AI eval > Run workflow, on `dev`: pick the feature,
   the model and the spend cap (default $10). Download the
   `ai-eval-report-...` artifact (the JSON report only).

## Running it locally

```bash
cd backend
pip install -r requirements-eval.txt
export AI_EVAL_R2_ENDPOINT=... AI_EVAL_R2_BUCKET=... \
       AI_EVAL_R2_ACCESS_KEY_ID=... AI_EVAL_R2_SECRET_ACCESS_KEY=...
# plus the provider's API key in its usual environment variable
python -m eval.ai_scan.run_eval --feature scan.standard \
    --model claude-haiku-5-5 --spend-cap-usd 10 --report ai-eval-report.json
```

`--provider` is optional (it follows from the model). It refuses to start
without `--spend-cap-usd`, without the four `AI_EVAL_R2_*` variables (or with
an endpoint that is not an `https://<account>.r2.cloudflarestorage.com` URL),
or without the provider's API key. Exit codes: 0 finished (also when the cap
stopped it), 1 aborted after 5 errored calls in a row
(`--max-consecutive-errors`), 2 refused or failed with a reason, 3 an
unexpected failure (only its type is printed, never its message).
