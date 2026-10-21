# Data integrity, server authority and AI cost

BookshelfAI holds people's libraries and pays for every AI call. These checks
are what a BC Arcade story never needed. Fill in the checklist in Phase 1 and
paste it into the PR's **Data & cost** section. In Phase 3, check the diff
against it: anything the diff does that the checklist doesn't declare is a
blocking finding.

## Checklist (PR "Data & cost" section)

```
## Data & cost
- Migration: none | additive | expand (of expand/contract) | contract | destructive (owner-approved: #N)
- User data deleted or overwritten: none | soft-delete with recovery window <N days> | hard delete (owner-approved: #N)
- Shared catalog (books/editions) written: no | yes, from server-fetched provider data only
- Client-supplied fields stored: none | user-scoped only (<table.columns>)
- AI calls: none | via gateway — feature <name>, model tier <haiku|sonnet>, max_tokens <N>, est. cost/call <$>, quota <rule>
- Storage growth: none | ~<bytes>/user/day, cap <rule>, retention <rule>
- Kill switch / dial: n/a | <setting name>, changeable without a release
- Rate limit: n/a | <setting name> on <endpoints>
```

Write "None — <reason>" only when every line is "none" or "n/a".

## Migrations

- **Expand/contract.** A rename or narrowing is three steps, in separate PRs:
  1. add the new column or table (expand)
  2. backfill and move reads/writes
  3. drop the old one (contract), with owner approval
- Every migration has a working `downgrade()`. CI runs upgrade, then
  `alembic check`, then `downgrade -1`.
- No raw SQL in app code. Data backfills inside a migration may use `op.execute`
  only when the ORM can't express them, and the PR says why.
- No `ON DELETE CASCADE` from shared tables (`books`, `editions`) into user
  tables. User rows are never collateral damage of a catalog change.
- Large backfills run in bounded batches, never one unbounded `UPDATE`.
- **Pre-launch note:** while there is no production user data, destructive
  changes are allowed without backfill shims. They still need expand/contract
  ordering if dev data must survive, and they always need the
  `owner-approval-required` label.

## User data

- Deleting a library item is a soft delete (`deleted_at`) with a recovery
  window and a bounded purge job. A hard delete needs an owner decision,
  except for account deletion, which is the user's explicit request.
- Account deletion and data export are complete. A new user-owned table must
  be added to both, with a test.
- Mutations from the client accept an `Idempotency-Key` where the endpoint
  pattern supports it, so client retries never double-write.

## Server authority

- The server decides and the client requests. Never trust client-sent:
  - user IDs, tier or entitlement, quota counts, prices
  - model names, prompts, token limits
  - catalog metadata for shared rows
- Shared catalog rows come only from server-side provider lookups (Open
  Library / Google Books). User-supplied title, notes or cover go in
  user-scoped columns.
- Validate cross-references server-side, e.g. that an `edition_id` belongs to
  its `book_id` and that the row belongs to the requesting user. Return 4xx,
  never a 500 from a bad FK.

## AI cost

- **Every model call goes through the gateway** (planned at `backend/app/ai/`):
  a feature registry entry, server-fixed model, prompt and `max_tokens`, image
  normalisation (decode-or-reject, strip EXIF, downscale), a per-user quota
  reserve then settle, the global ceiling, and the kill switch.
- Use the Anthropic SDK. Pin exact model IDs, set `max_tokens`, retry on
  429/5xx with backoff, handle `stop_reason == "refusal"`, and use structured
  outputs for anything parsed. `.claude/policies/claude.md` and the policy
  gate enforce part of this.
- Model output is untrusted: it is used only as search keys or candidates,
  never written to shared rows, and never logged raw.
- Pick the cheapest model that passes the eval for the feature (Haiku first).
  A tier change is a registry/config change plus an eval run, never a silent
  code edit.
- Tests must cover:
  - quota exhausted, which is refused before any provider call
  - kill switch off, which is refused before any provider call
  - concurrent requests from one user, where only one is in flight
  - provider error, where the quota reservation is released
  - the cost recorded in the usage ledger
- No AI calls in tests. Mock the provider at the gateway boundary.

## Storage growth (Supabase plan limits)

Any change that writes rows that grow with free usage needs these, and every
number lives in DB settings or env, not code:

- a byte estimate per user per day
- a cap (per-user and/or global)
- a retention rule (purge or roll up)
- a dial or kill switch
