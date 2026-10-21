# Database Backups

Daily encrypted backups of the production Supabase database to Cloudflare R2.
Two GitHub Actions workflows handle this: **DB Backup** (nightly) and
**DB Restore Drill** (manual, gated by required reviewers).

## Architecture

```
Supabase prod ──pg_dump──► gzip ──age encrypt──► R2 bucket
                                                  bookshelf/prod/<timestamp>.sql.gz.age
                                                  (30-day lifecycle rule)
```

The private age key never leaves your machine. GitHub only sees the public
recipient key (`BACKUP_AGE_RECIPIENT`) and the encrypted output.
The restore drill uses `BACKUP_AGE_IDENTITY` (the private key) stored as a
secret in the `backup-restore` environment, which requires a manual approval.

## Owner setup (one-time)

### 1. Cloudflare R2 bucket

1. Dashboard → R2 → **Create bucket** — name it e.g. `bookshelf-backups`;
   keep **Public access** off.
2. Bucket → **Settings** → **Lifecycle rules** → add a rule:
   - Prefix: `bookshelf/prod/`
   - Action: Delete objects after **30 days**
3. **R2 API tokens** → Create two tokens scoped to this bucket:
   - `bookshelf-backup-write` — **Object Read & Write** — for the backup job
   - `bookshelf-backup-read` — **Object Read** — for the restore drill
   Record each token's Access Key ID and Secret Access Key.
4. Note your Cloudflare **Account ID** (top-right of any R2 page).

### 2. age encryption key

On a trusted machine (not in CI):

```sh
age-keygen -o backup-key.txt
```

`backup-key.txt` contains both the public recipient key (`age1…`) and the
private identity key. Keep **two offline copies** of this file (e.g. USB
drives in separate locations). The private key is never in GitHub — only
the encrypted backups travel there.

### 3. Check Postgres major version

```sql
SELECT version();
```

Confirm it reports **PostgreSQL 17**. If it reports a different major version,
update `PG_MAJOR` in both `.github/workflows/backup.yml` and
`.github/workflows/backup-restore-drill.yml` before the first run.

### 4. GitHub environments and secrets

#### Environment: `backup`

Create at **Settings → Environments → New environment**:
- Name: `backup`
- Deployment branches: restrict to `dev` branch only

Add these secrets:

| Secret | Value |
|---|---|
| `BACKUP_DATABASE_URL` | Supabase session-pooler URL with `?sslmode=require` (port 5432) |
| `R2_ACCOUNT_ID` | Cloudflare Account ID |
| `R2_ACCESS_KEY_ID` | `bookshelf-backup-write` token's Access Key ID |
| `R2_SECRET_ACCESS_KEY` | `bookshelf-backup-write` token's Secret Access Key |
| `R2_BUCKET` | R2 bucket name (e.g. `bookshelf-backups`) |
| `BACKUP_AGE_RECIPIENT` | Public recipient key from `backup-key.txt` (the `age1…` line) |

#### Environment: `backup-restore`

Create at **Settings → Environments → New environment**:
- Name: `backup-restore`
- Deployment branches: restrict to `dev` and `main` branches
- **Required reviewers**: add at least one reviewer
- **Prevent self-review**: on

Add these secrets:

| Secret | Value |
|---|---|
| `BACKUP_AGE_IDENTITY` | Full contents of `backup-key.txt` (private key — handle carefully) |
| `R2_ACCOUNT_ID` | Same Cloudflare Account ID as above |
| `R2_BUCKET` | Same R2 bucket name |
| `R2_READ_ACCESS_KEY_ID` | `bookshelf-backup-read` token's Access Key ID |
| `R2_READ_SECRET_ACCESS_KEY` | `bookshelf-backup-read` token's Secret Access Key |

### 5. Verify

1. Run **DB Backup** manually (GitHub → Actions → DB Backup → Run workflow).
   It should complete in under two minutes and upload a `*.sql.gz.age` object
   to R2.
2. Run **DB Restore Drill** (Actions → DB Restore Drill → Run workflow).
   Approve the required-reviewer gate.
   The drill restores into a temporary in-runner Postgres instance, runs a
   row-count sanity check, and tears it down. A green run confirms the backup
   is restorable.
3. Check the R2 bucket to see the backup file.
4. Tick the corresponding boxes on issue #514.

## Runbook — emergency restore to production

> Only do this if production data is lost and you have no other path.

1. Download the latest backup: R2 → `bookshelf/prod/` → most recent file.
2. Decrypt locally: `age --decrypt -i backup-key.txt -o backup.sql.gz backup.sql.gz.age`
3. Decompress: `gunzip backup.sql.gz`
4. Restore to Supabase: `psql "$BACKUP_DATABASE_URL" < backup.sql`
   This will error on objects that already exist; use `--clean` to drop first
   if the schema is intact, or restore to a fresh Supabase project.
5. Verify with `SELECT COUNT(*) FROM users;` and a manual login.
