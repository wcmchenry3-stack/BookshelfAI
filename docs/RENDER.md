# Deploying to Render

The canonical source for service config is `render.yaml`. This doc explains the
dev/prod topology, the Supabase connection rules, and the first-prod-deploy
checklist. For the day-to-day release process (all platforms) see
[`deployment.md`](deployment.md).

## Topology

| Environment | Branch | API service          | API domain                   | Static site           | Site domain               | Database                               |
| ----------- | ------ | --------------------- | ----------------------------- | ---------------------- | -------------------------- | --------------------------------------- |
| Production  | `main` | `bookshelf-api-prod`  | `bookshelfapi.buffingchi.com` | `bookshelf-web-prod`   | `bookshelfai.buffingchi.com` | **Supabase** Postgres (session pooler) |
| Dev         | `dev`  | `bookshelf-api`        | `bookshelfapi-dev.buffingchi.com` | `bookshelf-web`     | (dev static site)          | Render Postgres `bookshelf-db` (dev only) |

All services are in Oregon.

## WARNING — do not Blueprint-sync this file

If a Render **Blueprint** is connected to this repo with auto-sync, merging
this `render.yaml` will create `bookshelf-api-prod` and `bookshelf-web-prod`
automatically — both are **billable** services. Check the Render dashboard
(Blueprints tab) before merging this change. The prod services in this repo
are meant to be created **manually** (see the checklist below), not applied
from the blueprint, because every secret is `sync: false` and a
blueprint-created service would boot without them and fail.

## Connecting to Supabase (production only)

Supabase is used as plain Postgres. The app does not use Supabase Auth,
Storage, or the Data API.

- **Session pooler only**: host `*.pooler.supabase.com`, port **5432**. Never
  the transaction pooler (port 6543) — it does not support the prepared
  statements asyncpg relies on. Never the direct host, which is IPv6-only and
  unreachable from Render (no outbound IPv6).
- The app normalizes `postgres://`/`postgresql://` to
  `postgresql+asyncpg://` itself (`Settings.async_database_url`) — paste the
  plain pooler URL, don't pre-convert it.
- **Enforce SSL: on** (Supabase project settings).
- **Data API: off**. Alembic's tables have no RLS policies — the Data API
  would expose them to anyone with the anon key.
- **No RLS, no Supabase Auth.** Alembic is the only schema tool; its
  `alembic upgrade head` pre-deploy step is what keeps prod's schema current.
- **Free plan pauses after ~1 week idle and has no backups.** Upgrade to Pro
  (daily backups) before store submission.

## Secrets policy

Every credential is `sync: false` in `render.yaml` and is pasted directly into
the Render dashboard by the owner. Secrets never go in this repo, a PR, a
chat message, or an MCP tool argument (including the Render MCP server —
paste values by hand in the dashboard UI instead). This applies to the
Supabase pooler URL, JWT keypair, Google OAuth credentials, the OpenAI key,
Turnstile, and Sentry DSNs.

## Health checks

- `GET /health` — no DB touch. This is `healthCheckPath` in `render.yaml`; a
  DB outage must not restart-loop the service.
- `GET /health/db` — DB-touching health check. **Not implemented yet** — it
  lands in a separate PR. Once it exists, point an external uptime monitor at
  it every 5 minutes; on Supabase's free plan this is also what stops the
  project from pausing due to inactivity.

## Deploys

- Push to `dev` → `bookshelf-api` and `bookshelf-web` auto-deploy as today.
- **Prod services have `autoDeploy: false`.** A push to `main` runs
  `.github/workflows/deploy.yml`: the full CI workflow first, then — only if
  green — a Render deploy and an OWASP ZAP baseline scan per service.
  `backend/tests/unit/test_render_yaml.py` guards `autoDeploy: false` staying
  set on both prod services.
- The workflow finds the prod services via two repository **variables** (not
  secrets — service IDs aren't sensitive): `RENDER_PROD_API_SERVICE_ID` and
  `RENDER_PROD_WEB_SERVICE_ID`. While a variable is unset, its deploy job is
  skipped and `preflight` posts a warning annotation — a green run with that
  warning means nothing was deployed.
- Merge promotions one at a time: Render deploys the *current* HEAD of `main`,
  not a pinned commit, so a second push while a deploy is in flight can ship
  ahead of its own CI run finishing.

## First production deploy — checklist

1. Create a Supabase project. Configure it per "Connecting to Supabase" above,
   then run `alembic upgrade head` against it once (export `DATABASE_URL`
   locally, run from `backend/`) to build the schema.
2. In the Render dashboard, create `bookshelf-api-prod` and
   `bookshelf-web-prod` **manually** using the values in `render.yaml`
   (branch `main`, auto-deploy off) — do NOT use Blueprint → Apply (see the
   WARNING above).
3. Paste every `sync: false` secret for both services in the dashboard,
   including the Supabase pooler URL as `DATABASE_URL` on
   `bookshelf-api-prod`.
4. Record each service's `srv-…` ID as the repository variables
   `RENDER_PROD_API_SERVICE_ID` / `RENDER_PROD_WEB_SERVICE_ID` (Settings →
   Secrets and variables → Actions → Variables).
5. Merge a `dev` → `main` promotion PR. Watch `.github/workflows/deploy.yml`
   go green.
6. Verify:
   - `curl https://bookshelfapi.buffingchi.com/health` → `{"status":"ok",...}`
   - once `/health/db` exists, verify it too and point the uptime monitor at
     it (every 5 minutes).
7. Upgrade Supabase to Pro (daily backups) before store submission.
