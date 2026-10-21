# Database backups and restore drill

Independent of Supabase's own backups: a nightly, client-side-encrypted
`pg_dump` of the production database is stored in Cloudflare R2. A manual,
approval-gated workflow proves the backups can actually be restored.

| Item | Value |
| --- | --- |
| What | Logical dump of the application schema only (`pg_dump -Fc -n public --no-owner --no-acl`) |
| Where | Cloudflare R2 bucket (secret `R2_BUCKET`), key `bookshelf/prod/YYYY/MM/DD/bookshelf-<UTC timestamp>.dump.age` |
| How often | Nightly at 07:17 UTC, plus manual `workflow_dispatch` |
| Encryption | [age](https://github.com/FiloSottile/age), encrypted to a public recipient key in CI; the private key is never in the backup job |
| Retention | 30 days, enforced by an R2 lifecycle rule (owner setup below) |
| Workflows | `.github/workflows/db-backup.yml`, `.github/workflows/db-restore-drill.yml` |

The plaintext dump only exists inside a pipe (`pg_dump | age > file`); only the
encrypted file touches the runner disk, and it is deleted at the end of the job.

**Schema scope.** All application tables live in the default `public` schema
(the Alembic migrations never create or target another schema), so the dump is
limited to `-n public`. Platform-owned schemas (auth, storage, realtime and so
on) are not backed up here; Supabase's own backups cover them.

**Public repository.** This repository is public, so workflow logs and job
summaries are world-readable. The workflows therefore print the object key and
pass/fail results only: no row counts and no byte sizes (the backup summary says
"size verified", the drill reports "users: non-empty" and similar).

## Secrets

Secrets are split by environment so that the production database URL and the
R2 write token are not available to every workflow in the repository.

Secrets of the **`backup` environment**, used by `db-backup.yml`:

| Secret | Purpose |
| --- | --- |
| `BACKUP_DATABASE_URL` | Postgres connection string for the production database (a read-only role is preferred). Use the Supabase **session pooler** URL (GitHub runners are IPv4-only), append `?sslmode=require`, and percent-encode special characters in the password (for example `@` as `%40`, `/` as `%2F`) |
| `R2_ACCOUNT_ID` | Cloudflare account id (used to build the S3 endpoint) |
| `R2_ACCESS_KEY_ID` | R2 API token access key (Object Read & Write, scoped to the one bucket) |
| `R2_SECRET_ACCESS_KEY` | R2 API token secret |
| `R2_BUCKET` | Bucket name |
| `BACKUP_AGE_RECIPIENT` | age **public** key (`age1...`) |

Secrets of the **`backup-restore` environment**, used by `db-restore-drill.yml`:

| Secret | Purpose |
| --- | --- |
| `BACKUP_AGE_IDENTITY` | age **private** key (`AGE-SECRET-KEY-...`). A multi-line identity file (comments plus the key line) also works, since the drill passes it to `age -i` |
| `R2_ACCOUNT_ID` | Cloudflare account id (same value as above) |
| `R2_BUCKET` | Bucket name (same value as above) |
| `R2_READ_ACCESS_KEY_ID` | Access key of a **separate, read-only** R2 API token (Object Read, same bucket) |
| `R2_READ_SECRET_ACCESS_KEY` | Secret of that read-only token |

Both workflows fail fast, naming any missing secret (never its value), so they
fail harmlessly until the owner setup below is done. There are no repository
level secrets for backups.

## Owner setup

1. **R2 bucket.** In Cloudflare, create a private R2 bucket (for example
   `bookshelf-backups`). Do not enable public access.
2. **Lifecycle rule.** In the bucket settings add an object lifecycle rule that
   deletes objects under prefix `bookshelf/prod/` 30 days after creation.
3. **API tokens.** Create two R2 API tokens limited to that bucket: one with
   Object Read & Write (backup job) and one with Object Read only (drill).
4. **age keypair.** On a trusted machine run `age-keygen -o backup-key.txt`.
   Put the printed public key (`age1...`) in the `backup` environment secret
   `BACKUP_AGE_RECIPIENT`. Keep the private key offline (password manager and
   an offline copy). Its only other home is the `backup-restore` environment
   secret `BACKUP_AGE_IDENTITY`. Losing the private key makes every backup
   unreadable, so store two copies.
5. **`backup` environment.** In GitHub, Settings > Environments, create
   `backup`. Under **Deployment branches and tags** choose *Selected branches
   and tags* and allow only `dev` (scheduled runs use the default branch, which
   must be `dev`). Add the `backup` secrets from the table above, including
   `BACKUP_DATABASE_URL`. Required reviewers are not set here, because the job
   runs unattended on a schedule.
6. **`backup-restore` environment.** Create `backup-restore` and:
   - restrict **Deployment branches and tags** to `dev` and `main`;
   - add **Required reviewers**;
   - enable **Prevent self-review**, so the person who triggers a drill cannot
     approve their own run;
   - add the `backup-restore` secrets from the table above.
7. **Failure notifications.** A failed scheduled run is reported by GitHub to
   the user who last changed the cron schedule in the workflow file, according
   to that user's Actions notification settings (Settings > Notifications >
   Actions). Confirm that this is the owner and that notifications for failed
   workflows are on; otherwise failures are silent. The restore drill also warns
   when the newest backup is older than 36 hours, which catches a backup that
   has stopped running.
8. **Postgres major version.** Run `select version();` against production. Set
   `PG_MAJOR` in both workflows to that major (or higher; the dump client must
   not be older than the server). The default is 17.
9. Trigger **DB Backup** manually once and confirm the job summary shows the
   key and "size verified".

## Unpinned inputs (accepted)

Two inputs are deliberately not pinned: the `age` package installed from the
Ubuntu apt repository, and the `postgres:<major>` image tag (which moves with
minor releases). Both are accepted for a backup job; the major version is the
only value that matters for compatibility and it is set explicitly.

## Manual restore

On a trusted machine with `age` and `pg_restore` (same major as the dump):

```bash
# 1. Download the object (use the R2 S3 endpoint)
aws s3 cp "s3://<bucket>/bookshelf/prod/YYYY/MM/DD/bookshelf-<timestamp>.dump.age" . \
  --endpoint-url "https://<account-id>.r2.cloudflarestorage.com"

# 2. Decrypt with the offline private key
age -d -i backup-key.txt -o restore.dump bookshelf-<timestamp>.dump.age

# 3. Restore into an empty database
createdb bookshelf_restore
pg_restore --no-owner --no-acl -d bookshelf_restore restore.dump

# 4. Sanity check
psql -d bookshelf_restore -c "select version_num from alembic_version"
```

Delete `restore.dump` and any copy of the key when finished. To recover
production, restore into a fresh database, verify, then repoint
`DATABASE_URL`; do not restore over a live database.

## Restore drill

Run **DB Restore Drill** (Actions > DB Restore Drill > Run workflow) at least
**quarterly**, and after any change to the backup workflow, Postgres major
version or key. A reviewer must approve the `backup-restore` environment. The
job starts a throwaway Postgres container (`docker run postgres:<PG_MAJOR>`,
removed in the cleanup step), downloads the newest backup with the read-only
token, decrypts it, restores it, and checks the result.

Optional input `expected_revision`: if set, the drill fails unless the restored
`alembic_version` equals it.

The drill fails when any of these is true:

- `alembic_version` is missing or empty;
- `users`, `books`, `editions` or `user_books` is missing;
- `public.users` has no rows (production is never empty);
- `expected_revision` was given and does not match;
- `pg_restore` reports an error that is not on the allowlist below.

It warns (`::warning::` and a summary line) when the newest backup object is
older than 36 hours.

`pg_restore` stderr is written to a temporary file (deleted in cleanup). Only
the count of tolerated and unexpected errors is printed, plus the unexpected
error lines. The tolerated (known-benign hosted-platform noise) allowlist is:

- `schema "public" already exists` (the dump includes `CREATE SCHEMA public`);
- `role "..." does not exist`;
- `extension "..." is not available`, `could not open extension control file`,
  `permission denied for extension` / `permission denied to create extension`;
- errors naming a Supabase-owned schema: `auth`, `storage`, `realtime`,
  `extensions`, `graphql`, `graphql_public`, `vault`, `pgsodium`,
  `supabase_functions`, `_realtime`, `net`, `pgbouncer`.

Anything else fails the drill. The allowlist lives in the `benign` variable of
the "Decrypt and restore" step; keep this list and that variable in sync.

## Cost

One compressed, encrypted dump per day, kept 30 days: storage is roughly
`dump size x 30`. At the current size (small, pre-launch) this is far inside
R2's free tier (10 GB-month storage, free egress). The size is deliberately not
printed in the public job summary, so check the object size in the Cloudflare
dashboard as data grows; if `size x 30` approaches the free tier, shorten
retention in the lifecycle rule or reduce the schedule. GitHub Actions minutes
are a few minutes per night.
