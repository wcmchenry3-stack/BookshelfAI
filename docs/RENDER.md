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

## Environment values

`ENVIRONMENT` drives `Settings.is_hardened` (#448): everything except
`development`/`test` is hardened (TrustedHost enforcement, `/docs` disabled,
HSTS, `/auth/test-login` unregistered, and boot fails if `TEST_AUTH_SECRET`
is set). Which client-IP header is trusted is a
separate setting, `TRUSTED_PROXY_MODE` (see below).

| Value         | Hardened? | Used by                                      |
| ------------- | --------- | --------------------------------------------- |
| `development` | no        | local dev only — never set on a Render service |
| `test`        | no        | CI/pytest only                                 |
| `staging`     | yes       | `bookshelf-api-dev` — exercises the prod path  |
| `production`  | yes       | `bookshelf-api`                                |

## Client IP and origin lockdown

Full description: [`security.md` → Client IP and origin lockdown](security.md#client-ip-and-origin-lockdown).

| Variable | Values | Set where |
| --- | --- | --- |
| `TRUSTED_PROXY_MODE` | `cloudflare` or `none`; default: `cloudflare` in hardened environments, `none` otherwise; anything else fails boot | `render.yaml` sets `cloudflare` explicitly on both API services |
| `ORIGIN_AUTH_HEADER` | header name, default `X-Origin-Auth` | leave unset unless the edge uses another name |
| `ORIGIN_AUTH_SECRET` | empty (default) = lockdown off; otherwise at least 32 printable ASCII characters (e.g. `openssl rand -hex 32`) or boot fails | dashboard secret (`sync: false`) |

Both API start commands carry `--no-proxy-headers` so uvicorn never rewrites
the client address from `X-Forwarded-For`
(`backend/tests/unit/test_render_yaml.py` checks this). This file is not
blueprint-synced, so copy the start command into each service's dashboard
settings by hand.

Rollout, per service (dev first, then prod):

1. Optionally confirm `TRUSTED_PROXY_MODE` in the dashboard (not required:
   the default is `cloudflare` in hardened environments). Update the start
   command to the one in `render.yaml` (adds `--no-proxy-headers`, defence in
   depth).
2. Deploy, with `ORIGIN_AUTH_SECRET` unset.
3. The owner adds the edge rule that sends the origin header on requests to
   the API hostname.
4. Set `ORIGIN_AUTH_SECRET` in the dashboard to the same value. Check that the
   public hostname still works and that `/health` still passes Render's health
   check.

To roll back, clear `ORIGIN_AUTH_SECRET` (lockdown off) and/or set
`TRUSTED_PROXY_MODE=none`.

## Health checks

**Depends on PR #448 (health-check split).** This section describes the
post-#448 behavior; until #448 is merged and deployed, `/health` still
touches the DB itself (no separate `/health/db` exists yet).

- `GET /health` — liveness only, no DB touch. This is `healthCheckPath` in
  `render.yaml` and is exempt from both TrustedHost and the origin lockdown
  so Render's own prober can hit the origin directly; a DB outage must not restart-loop the service.
- `GET /health/db` — `SELECT 1` against the database, bounded at a 5 second
  timeout so a stalled pooler yields a prompt response; returns `200` (ok) or
  `503` (unavailable/unconfigured); rate limited. It is **not**
  exempt from TrustedHost or the origin lockdown, so probe it only via the public hostname
  (`https://bookshelfapi.buffingchi.com/health/db`), never the Render origin.
  Point an external uptime monitor at it every 5 minutes; on Supabase's free
  plan this is also what stops the project from pausing due to inactivity.

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
- Each deploy job passes `commit-id: ${{ github.sha }}` to the shared
  `called-deploy-render.yml`, pinning the Render deploy to the exact commit
  this run's CI validated — a second push landing mid-run cannot cause an
  unvalidated commit to ship.
- `deploy-web` needs both `ci` and `deploy-api`: the API's `preDeployCommand`
  runs `alembic upgrade head`, so web must never ship against a failed API
  deploy. Its `if:` still runs when `deploy-api` was merely *skipped* (blank
  service-ID var) — only an actual API deploy failure blocks it.

## Resolving a sign-in `account_conflict` (409)

`POST /auth/google` answers `409 account_conflict` when a Google account the
API has never seen (new `sub`) presents an email, ignoring case, that already
belongs to a user with a different `google_id` (#688). The app never merges
the two; an operator does, by hand:

1. Find the holder: the API log line `Sign-in rejected: email belongs to a
   user with a different subject (holder_user_id=<old_id>)`. Logs never carry
   the email.
2. **Verify ownership out of band** before changing anything: the person must
   show they control both the old account (sign in with the old Google
   account, or confirm from that mailbox) and the new Google account. If in
   doubt, do nothing — the 409 is the safe state.
3. Choose one fix. The SQL below is **operator-only**, run in a transaction
   in the database console (Supabase SQL editor / `psql`); application code
   stays ORM-only.

   **A. Move the existing library to the new Google account.** Free the email
   (as in B), ask the person to sign in once with the new Google account
   (this creates an empty user; its id is in the `Signed-in user resolved
   (user_id=<new_id>)` log line), then hand its subject to the old row:

   The guard runs inside the transaction: if the new account already holds
   user content (`user_books`, `user_custom_books`, `scan_records`), the
   block raises and nothing is deleted — stop and merge by hand. Its
   `ai_quota`, `ai_inflight`, `ai_enhanced_zero_results` and `refresh_tokens`
   rows are disposable for a just-created account and cascade with it
   (`ai_usage` rows are kept with `user_id` set to NULL).

   ```sql
   -- OPERATOR ONLY. Replace <old_id> and <new_id> (both lines).
   BEGIN;
   SELECT id, google_id, email, created_at FROM users
    WHERE id IN ('<old_id>', '<new_id>');
   DO $$
   DECLARE
     old_id uuid := '<old_id>';
     new_id uuid := '<new_id>';
     moving_sub text;
     moving_email text;
   BEGIN
     IF (SELECT count(*) FROM users WHERE id IN (old_id, new_id)) <> 2 THEN
       RAISE EXCEPTION 'both users must exist; stop';
     END IF;
     IF (SELECT count(*) FROM user_books WHERE user_id = new_id)
      + (SELECT count(*) FROM user_custom_books WHERE user_id = new_id)
      + (SELECT count(*) FROM scan_records WHERE user_id = new_id) > 0 THEN
       RAISE EXCEPTION 'new account has data; stop and merge manually';
     END IF;
     SELECT google_id, email INTO moving_sub, moving_email
       FROM users WHERE id = new_id;
     DELETE FROM users WHERE id = new_id;  -- cascades its disposable rows
     UPDATE users SET google_id = moving_sub, email = moving_email
      WHERE id = old_id;
   END $$;
   COMMIT;  -- after a RAISE this only rolls back
   ```

   Optional, recommended: sign out every existing session of the old row, so
   devices still signed in through the old Google account lose access now
   that the row belongs to the new one:

   ```sql
   -- OPERATOR ONLY.
   DELETE FROM refresh_tokens WHERE user_id = '<old_id>';
   ```

   Access tokens already issued stay valid until they expire
   (`JWT_EXPIRY_HOURS`).

   **B. Free the email only** (the old account keeps its library and stays
   reachable through its own Google account; the new Google account gets a
   fresh account on its next sign-in):

   ```sql
   -- OPERATOR ONLY. Replace <old_id>.
   BEGIN;
   UPDATE users SET email = 'released+' || id || '@invalid.example'
    WHERE id = '<old_id>';
   COMMIT;
   ```

   If the old Google account signs in later, its stored email is refreshed
   from its token again (when no other user holds that address).
4. Ask the person to sign in again and confirm it succeeds.

## First production deploy — cutover checklist

**Requires #448 (health split) merged and deployed before cutover** — until
then `/health` touches the DB itself, so a paused/unreachable Supabase project
would fail `/health` and restart-loop the API instead of degrading gracefully.

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
3. Once `bookshelf-api-dev` exists, repoint `backend-url` in
   `.github/workflows/ci.yml`'s `backend-health` job at its Render origin
   (`*.onrender.com`) — CI should probe dev, not the future-prod origin.
4. **Only then switch the existing services to prod:** `bookshelf-api` /
   `bookshelf-web` — branch → `main`, `autoDeploy: false`; on `bookshelf-api`,
   `DATABASE_URL` → the Supabase session-pooler URL (remove the Render
   `bookshelf-db` link), `ENVIRONMENT=production`, `healthCheckPath: /health`;
   generate a **fresh production JWT keypair** — never reuse dev's — and set
   `JWT_PRIVATE_KEY`/`JWT_PUBLIC_KEY`. Prod DNS and hostnames do not change.
5. Record each service's `srv-…` ID as `RENDER_PROD_API_SERVICE_ID` /
   `RENDER_PROD_WEB_SERVICE_ID`, merge a `dev` → `main` promotion PR, and
   watch `.github/workflows/deploy.yml` go green.
6. Verify `curl https://bookshelfapi.buffingchi.com/health` and `/health/db`;
   point an uptime monitor at `/health/db` (every 5 min, public hostname
   only). Upgrade Supabase to Pro (daily backups) before store submission.
