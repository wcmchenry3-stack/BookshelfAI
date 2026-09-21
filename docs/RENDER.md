# Deploying to Render

The canonical source for service config is `render.yaml`. This doc explains the
dev/prod topology, the Supabase connection rules, and the first-prod-deploy
cutover checklist. For the day-to-day release process (all platforms) see
[`deployment.md`](deployment.md).

## Topology

| Environment | Branch | API service          | API domain                        | Static site         | Site domain                   | Database                                  |
| ----------- | ------ | --------------------- | ----------------------------------- | --------------------- | ------------------------------- | ------------------------------------------ |
| Production  | `main` | `bookshelf-api`        | `bookshelfapi.buffingchi.com`       | `bookshelf-web`        | `bookshelfai.buffingchi.com`     | **Supabase** Postgres (session pooler)    |
| Dev         | `dev`  | `bookshelf-api-dev`    | `bookshelfapi-dev.buffingchi.com`   | `bookshelf-web-dev`    | (new dev site — DNS TBD)         | Render Postgres `bookshelf-db` (dev only) |

All services are in Oregon. `bookshelf-api`/`bookshelf-web` are the pre-existing
services — their hostnames are already live and do not change.
`bookshelf-api-dev`/`bookshelf-web-dev` are new. `bookshelfapi-dev.buffingchi.com`
does not resolve until the dev pair is created and DNS is added (see checklist).

## WARNING — do not Blueprint-sync this file

If a Render **Blueprint** is connected to this repo with auto-sync, merging
this `render.yaml` will create `bookshelf-api-dev` and `bookshelf-web-dev`
automatically — both are **billable** services. Check the Render dashboard
(Blueprints tab) before merging this change. Both the new dev services and the
`bookshelf-api`/`bookshelf-web` cutover in the checklist below are meant to be
done **manually**, not applied from the blueprint — every secret is
`sync: false` and a blueprint-created service would boot without them and fail.

## CAUTION — read before touching anything

**Until checklist step 3, `bookshelfapi.buffingchi.com` (prod) still serves the
`dev` branch against the existing Render `bookshelf-db`.** Native release
builds compiled from `frontend/.env.production` already point at that
hostname, so they're talking to dev code/data right now. Do the dev cutover
(step 2) first and verify it end to end before step 3.

**Step 3 replaces prod's database.** Once `DATABASE_URL` on `bookshelf-api`
switches to the fresh Supabase project, every account/library currently on the
prod hostname starts **empty** — by design (a clean production start), not
data loss: the old data stays reachable on the dev stack
(`bookshelfapi-dev.buffingchi.com`, still on `bookshelf-db`).

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

- **Prod (`bookshelf-api`, `bookshelf-web`) will have `autoDeploy: false`** once
  cut over. A push to `main` runs `.github/workflows/deploy.yml`: CI first,
  then — only if green — a Render deploy and an OWASP ZAP baseline scan per
  service. `backend/tests/unit/test_render_yaml.py` guards `autoDeploy: false`.
- The workflow finds the prod services via two repository **variables** (kept
  as `RENDER_PROD_API_SERVICE_ID` / `RENDER_PROD_WEB_SERVICE_ID` even though
  they now point at `bookshelf-api`/`bookshelf-web`). An unset variable skips
  its deploy job and `preflight` posts a warning — a green run with that
  warning means nothing deployed.
- Merge promotions one at a time: Render deploys the *current* HEAD of `main`,
  not a pinned commit, so a second push mid-deploy can ship ahead of its CI.

## First production deploy — cutover checklist

1. Create a Supabase project and harden it per "Connecting to Supabase" above.
2. **Stand up dev first, verify it, before touching prod:** create
   `bookshelf-api-dev` and `bookshelf-web-dev` by hand on branch `dev` using
   the values in `render.yaml`; attach the existing Render Postgres
   `bookshelf-db` to `bookshelf-api-dev` (`fromDatabase` already expects
   this); copy the current dashboard secrets from the pre-cutover
   `bookshelf-api`/`bookshelf-web` across; add Cloudflare DNS for
   `bookshelfapi-dev.buffingchi.com` and a dev web hostname (both proxied)
   plus the matching Render custom domains; add the new dev web origin to the
   Google OAuth client's authorised origins; verify the dev pair end to end
   (login, scan, health) before step 3.
3. **Only then switch the existing services to prod:** `bookshelf-api` /
   `bookshelf-web` — branch → `main`, `autoDeploy: false`; on `bookshelf-api`,
   `DATABASE_URL` → the Supabase session-pooler URL (remove the Render
   `bookshelf-db` link), `ENVIRONMENT=production`, `healthCheckPath: /health`;
   generate a **fresh production JWT keypair** — never reuse dev's — and set
   `JWT_PRIVATE_KEY`/`JWT_PUBLIC_KEY`. Prod DNS and hostnames do not change.
4. Record each service's `srv-…` ID as `RENDER_PROD_API_SERVICE_ID` /
   `RENDER_PROD_WEB_SERVICE_ID`, merge a `dev` → `main` promotion PR, and
   watch `.github/workflows/deploy.yml` go green.
5. Verify `curl https://bookshelfapi.buffingchi.com/health` and, once it
   exists, `/health/db`; point an uptime monitor at `/health/db` (every 5
   min). Upgrade Supabase to Pro (daily backups) before store submission.
