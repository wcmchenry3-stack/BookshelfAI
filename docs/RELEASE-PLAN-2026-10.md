# BookshelfAI — Release Readiness Plan (infra phase, target: store submission late Oct 2026)

> Live resource IDs, audit detail and product strategy are kept in the private notes repo, not here.

## Context

BookshelfAI's supporting infrastructure fell behind its sibling project while features stalled. This phase
gets the app _releasable_: a passing iOS archive, a real production environment, working observability, and
the CI / release tooling already proven on the sibling project. Product gaps (Settings screen, subscription /
paywall, notes + tags UI) are a **second plan** that starts once workstreams 1–3 land.

### Decisions (owner, 2026-09-20)

1. **Production database = Supabase; dev database stays on Render; local + CI stay on SQLite.** Supabase is
   Postgres only — no Supabase Auth, Storage, Data API, RLS, CLI or branching. Google sign-in + RS256 JWTs are
   unchanged. **Alembic is the only schema path.** Prod is a **fresh database**, not a migration.
2. **Two environments on Render.** The existing API + web services already own the production hostnames, so
   they **become prod**: they move to `main`, auto-deploy off, deploying only through `deploy.yml` after CI is
   green. A new `-dev` pair tracks the `dev` branch and takes over the Render Postgres database. (Amended
   2026-09-20 — the first draft had the existing services staying dev.)
3. **Submission is sequenced after the sibling app's October submission** — late October.
4. **Server-authoritative.** The server owns all library state. The _only_ offline feature is capturing scan
   images and queueing them for upload. No optimistic writes, no client-side source of truth, no persisted
   query cache for authoritative data.

## Workstreams

### WS1 — iOS archive failure (Xcode 27)

Xcode Cloud moved to Xcode 27, which turns deployment targets below 15.0 into a hard error. Two CocoaPods
resource-bundle targets (`Sentry-Sentry`, `RNCAsyncStorage-RNCAsyncStorage_resources`) inherit their podspec
minimum rather than the Podfile platform, so the archive failed. Fix: normalise pod deployment targets in
`post_install` (PR #446). Follow-ups: pin the Xcode Cloud workflow to an explicit Xcode version; see
`docs/claude/ios-ci.md` § Xcode version rollovers.

### WS2 — Supabase model + production topology

| Piece | Where |
|---|---|
| `/health` is DB-free (Render liveness); new `/health/db` for uptime monitoring; bounded pre-ping pool | PR #448 |
| `render.yaml` → prod (existing names) + new `-dev` services, prod `DATABASE_URL` is a dashboard secret, guard tests | `feature/render-prod-topology` |
| `deploy.yml` repaired (it referenced the repo's pre-rename name and never ran) | same branch |
| Topology, connection rules, first-deploy checklist | `docs/RENDER.md` (same branch) |

Connection rules: **session pooler, port 5432** (Render has no outbound IPv6, so the direct host is
unreachable; the transaction pooler on 6543 breaks asyncpg). SSL enforced on the Supabase side; Data API off.
**Secrets live only in the Render dashboard** — never in this repo, in chat, or in tool arguments.

Owner-only steps: create the Supabase project and harden it in the dashboard → create the new `-dev` Render
services by hand (not Blueprint sync), attach the Render database and `-dev` DNS, and prove dev works → only then
switch the existing services to `main` + the Supabase URL + a fresh prod JWT keypair → set repo variables `RENDER_PROD_API_SERVICE_ID` /
`RENDER_PROD_WEB_SERVICE_ID` → merge `dev` → `main` → verify `/health` + `/health/db` → 5-minute uptime monitor
on `/health/db` (keeps a free-plan project from pausing) → upgrade Supabase to Pro before submission.

### WS3 — Observability and edge audit

- **Sentry:** confirm events actually arrive from API, web and a TestFlight build (send one test event each);
  add `release` tagging on the backend and source-map / dSYM upload from Xcode Cloud; PII scrubbing via
  `EventScrubber`; drop simulator events. One project, split dev/prod by the `environment` tag.
- **Render:** reconcile the live dev services with `render.yaml` (health-check path, and a web build command
  that writes _all_ `EXPO_PUBLIC_*` values into the bundle).
- **Cloudflare:** DNS for the prod + dev hostnames proxied to Render, SSL Full (strict), Bot Fight Mode
  compatibility with CI health checks, and the Cloudflare Web Analytics beacon (with matching CSP entries).
- **Secret scanning:** restore gitleaks' default rules (PR #447). Full-history scan is clean.

### WS4 — Release tooling ported from the sibling project

Diff against the existing `ci.yml` first — it already has `podfile-lock-check`, `gradle-wrapper-check`,
`local-path-check`, `test-migrations`, `js-bundle-check` and both native build checks.

- Claude hooks: `commit-gate`, `precheck-gate`, `pr-title-gate`.
- CI: conflict-marker check, `sentry-cli-check`, native-change detection to gate the native builds.
- `perf.yml`: Locust against scan / search / books, Lighthouse CI on the web export.
- E2E: Playwright against the web export (auth, scan upload, my-books, wishlist, axe); Maestro smoke on `main`.
- Scripts: i18n completeness check, bundle-size budget, build-env validation.
- Docs: store privacy answers, ATT audit, accessibility statement, native-dependency PR checklist.

**Not ported:** the sibling's offline-first layer (event store, sync worker, score queue, offline entitlement
grace). See decision 4.

**Offline capture queue (exists — harden only):** `app/(tabs)/scan.tsx` copies captures into the app's document
directory; `contexts/ScanJobContext.tsx` queues when offline and drains on reconnect; `lib/scanJobStorage.ts`
persists job metadata. To add: a queue cap and visible pending count, an orphan-file sweep, kill/relaunch +
drain tests, and explicit online-only behaviour (with clear UI) for every library mutation.

## Sequencing

| When | Work |
|---|---|
| Sep 21–25 | WS1 merged + Xcode Cloud archive green · WS3 quick wins · issues filed per workstream |
| Sep 28–Oct 2 | WS2 code merged (health split, pool, render.yaml + guards, deploy.yml) |
| Oct 5–9 | Owner ops only: Supabase project, prod Render services, DNS |
| Oct 12–23 | WS4 ports · first prod deploy · TestFlight + Play internal builds · Supabase Pro |
| Then | Product plan (Settings, paywall, notes/tags) → store submission |

## Verification

- **WS1:** `js-bundle-check` + `ios-build-check` green, then an Xcode Cloud archive succeeds and
  `ci_post_xcodebuild.sh` finds `main.jsbundle`. CI alone cannot prove this fix — it builds Debug on the
  runner's default Xcode.
- **WS2:** `pytest tests/ --cov=app` ≥ 80 %; after the first prod deploy `/health` → 200 without the DB and
  `/health/db` → 200; prod tables exist and match the dev Alembic head; Supabase advisors show no ERROR; the
  dev API still reads the Render database.
- **WS3:** three Sentry test events visible with correct `environment` + `release`; a native crash
  symbolicates; a Web Analytics pageview registers; ZAP scan green against the prod API.
- **WS4:** Jest ≥ 80 %; Playwright green in CI; hooks reject a bad commit / PR title locally; airplane-mode test
  — capture 3 scans → kill the app → relaunch → reconnect → all 3 upload and their files are removed.
- Every change: draft PR → `dev`, all CI green, owner merges.
