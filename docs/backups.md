# Database backups and restore drill

Independent of Supabase's own backups: a nightly, client-side-encrypted
`pg_dump` of the production database is stored in Cloudflare R2. A manual,
approval-gated workflow proves the backups can actually be restored.

| Item | Value |
| --- | --- |
| What | Full logical dump (`pg_dump -Fc --no-owner --no-acl`) of the production database, all schemas the connection can read |
| Where | Cloudflare R2 bucket (secret `R2_BUCKET`), key `bookshelf/prod/YYYY/MM/DD/bookshelf-<UTC timestamp>.dump.age` |
| How often | Nightly at 07:17 UTC, plus manual `workflow_dispatch` |
| Encryption | [age](https://github.com/FiloSottile/age), encrypted to a public recipient key in CI; the private key is never in CI backup jobs |
| Retention | 30 days, enforced by an R2 lifecycle rule (owner setup below) |
| Workflows | `.github/workflows/db-backup.yml`, `.github/workflows/db-restore-drill.yml` |

The plaintext dump only exists inside a pipe (`pg_dump | age > file`); only the
encrypted file touches the runner disk, and it is deleted at the end of the job.

## Secrets

Repository secrets used by `db-backup.yml`:

| Secret | Purpose |
| --- | --- |
| `BACKUP_DATABASE_URL` | Postgres connection string for the production database (a read-only role is preferred). Use the Supabase **session pooler** URL, because GitHub runners are IPv4-only |
| `R2_ACCOUNT_ID` | Cloudflare account id (used to build the S3 endpoint) |
| `R2_ACCESS_KEY_ID` | R2 API token access key, scoped to the one bucket |
| `R2_SECRET_ACCESS_KEY` | R2 API token secret |
| `R2_BUCKET` | Bucket name |
| `BACKUP_AGE_RECIPIENT` | age **public** key (`age1...`) |

Secrets of the `backup-restore` **environment** (not repository secrets), used
by `db-restore-drill.yml`:

| Secret | Purpose |
| --- | --- |
| `BACKUP_AGE_IDENTITY` | age **private** key (`AGE-SECRET-KEY-...`) |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | Same values as above (a read-only token is enough here) |

Both workflows fail fast, naming any missing secret (never its value), so they
fail harmlessly until the owner setup below is done.

## Owner setup

1. **R2 bucket.** In Cloudflare, create a private R2 bucket (for example
   `bookshelf-backups`). Do not enable public access.
2. **Lifecycle rule.** In the bucket settings add an object lifecycle rule that
   deletes objects under prefix `bookshelf/prod/` 30 days after creation.
3. **API token.** Create an R2 API token with Object Read & Write limited to
   that bucket. Store its access key id and secret as `R2_ACCESS_KEY_ID` and
   `R2_SECRET_ACCESS_KEY`; store the account id as `R2_ACCOUNT_ID` and the
   bucket name as `R2_BUCKET`.
4. **age keypair.** On a trusted machine run `age-keygen -o backup-key.txt`.
   Put the printed public key (`age1...`) in the repository secret
   `BACKUP_AGE_RECIPIENT`. Keep the private key offline (password manager and
   an offline copy). Its only other home is the `backup-restore` environment
   secret `BACKUP_AGE_IDENTITY`. Losing the private key makes every backup
   unreadable, so store two copies.
5. **Database URL.** Add `BACKUP_DATABASE_URL` as a repository secret.
6. **Environment.** In GitHub, Settings > Environments, create
   `backup-restore`, add required reviewers, and add the secrets listed above
   for that environment.
7. **Postgres major version.** Run `select version();` against production. Set
   `PG_MAJOR` in both workflows to that major (or higher; the dump client must
   not be older than the server), and update the `postgres:<major>` service
   image in `db-restore-drill.yml` to match. The default is 17.
8. Trigger **DB Backup** manually once and confirm the job summary shows the
   key and a verified size.

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
job downloads the newest backup, decrypts it, restores into a throwaway
Postgres service container, and writes the `alembic_version` revision and row
counts for `users`, `books`, `editions`, `user_books` and `refresh_tokens` to
the job summary. It fails if `alembic_version` is missing or empty. A non-zero
`pg_restore` exit code is reported in the summary; review the step log to
confirm skipped objects are only hosted-platform extras.

## Cost

One compressed, encrypted dump per day, kept 30 days: storage is roughly
`dump size x 30`. At the current size (small, pre-launch) this is far inside
R2's free tier (10 GB-month storage, free egress). Re-check the size in the
job summary as data grows; if `size x 30` approaches the free tier, shorten
retention in the lifecycle rule or reduce the schedule. GitHub Actions minutes
are a few minutes per night.
