# Testing

How Bookshelf is tested: the split between unit / integration / security / accuracy / performance, conftest patterns, and how to run + write new tests. For the Claude-specific standards file (coverage thresholds, rollback fixture conventions) see [`claude/testing.md`](claude/testing.md).

## Philosophy

- **Unit tests mock the DB** — they're fast and run on every PR.
- **Integration tests hit a real Postgres** and are auto-skipped when `DATABASE_URL` points at the dummy URL used by CI's default config. CI runs them in the `test-migrations` job, which has a Postgres 16 service.
- **Security tests are a separate marker** — `pytest -m security` — so the OWASP-aligned suite can be run standalone and surfaced in its own workflow (`security.yml`). `pytest.ini` has no marker filter, so they also run with everything else in `test-python` on every PR. See "When the security suite runs" below.
- **Accuracy and performance suites** live beside the core tests but run opt-in (benchmarks are skipped by default).
- **Don't mock what you can rollback** — when testing DB interactions, prefer the savepoint-based `db_session` fixture over mock sessions. Feedback memory flagged a real production incident where mocked DB tests passed and the actual migration broke prod.

## Backend (pytest)

**Location:** `backend/tests/`
**Config:** `backend/pytest.ini` — `asyncio_mode = auto`, registered markers (`security`, `benchmark`)
**Fixtures:** `backend/tests/conftest.py`

### Structure

```
backend/tests/
├── conftest.py          DATABASE_URL guard, rate-limit reset fixture, db_session fixture
├── unit/                Per-module tests — mock DB via fixtures
├── integration/         Real-DB tests — @pytest.mark.integration, auto-skipped w/o DATABASE_URL
├── limits/              Scan limits matrix on real Postgres — @pytest.mark.integration (test_no_real_provider.py is unmarked)
├── security/            OWASP tests — @pytest.mark.security
├── accuracy/            Dedup + enrichment correctness benchmarks
└── performance/         @pytest.mark.benchmark (pytest-benchmark)
```

### Key fixtures

**`_reset_rate_limits`** (autouse) — clears slowapi's in-memory `MemoryStorage` before every test. Without this, earlier tests eat into the rate-limit budget of later tests and you get spurious 429s.

**`db_session`** — async session with savepoint-based isolation:
```python
async def test_create_user(db_session: AsyncSession):
    user = User(email="t@test.com", google_id="abc")
    db_session.add(user)
    await db_session.flush()
    # ... assertions
    # implicit rollback at end of test — DB state never persists
```

**DB guard** — `pytest_collection_modifyitems` auto-skips tests tagged `@pytest.mark.integration` when `DATABASE_URL` isn't a real Postgres URL. That keeps CI fast on the default Python-test job while the integration job passes its own real DB URL.

Integration tests can live in any folder (`tests/integration/`, `tests/limits/`, `tests/security/`, `tests/unit/`); the marker, not the folder, decides. The `test-migrations` job runs `pytest -m integration tests -q -rs` (all folders), so the summary line shows how many ran and `-rs` lists every skip. It also sets `REQUIRE_DB_TESTS=1`: with that variable set, `tests/_db_guard.py` makes `conftest.py` abort the run with an error if `DATABASE_URL` is missing or the dummy URL, so a mass skip can never pass (#760). `tests/unit/test_db_guard.py` covers the guard.

### Running

```bash
cd backend && source .venv/bin/activate

# Full suite (integration auto-skipped without a real DB)
pytest

# With coverage report
pytest --cov=app --cov-report=term-missing

# Enforce the CI threshold (app code only via backend/.coveragerc, compared to 2 decimal places; #621)
pytest --cov=. --cov-fail-under=98

# Single file
pytest tests/unit/test_health_host_exemption.py

# Single test
pytest tests/unit/test_health_host_exemption.py::test_health_accepts_untrusted_host_header

# Security suite only (the marker run in security.yml)
pytest -m security

# Integration tests (against a real DB)
DATABASE_URL=postgresql+asyncpg://localhost/bookshelf_test pytest -m integration
```

### When the security suite runs

`security`-marked tests run on every PR as part of the plain `pytest` in `test-python` (`backend/pytest.ini` sets no marker filter). They also run on their own, as `pytest -m security`, in `.github/workflows/security.yml` (job `pytest-security`, with `TURNSTILE_REQUIRED=false`, next to Bandit and ZAP) when:

- a pull request targets `dev` and changes `backend/app/**`, `backend/tests/security/**`, `backend/requirements*.txt`, `.github/workflows/security.yml` or `.zap/**`;
- on the weekly schedule (Monday 08:00 UTC); or
- on manual dispatch.

A PR that changes none of those paths, or a PR to `main`, skips the separate `security.yml` run, but still runs the security tests in `test-python`.

### Nightly scan abuse load run (#539)

`backend/perf/` holds a Locust load run of `POST /scan` against a **fake provider**. It proves, under concurrent load, that the limits hold: the daily allowance, the in-flight lock, the cost breaker and the kill switch. It is not a latency benchmark and never calls a real model. It runs from `.github/workflows/perf-ai.yml` every night (03:17 UTC) and on demand (`workflow_dispatch`); it is **not a required check** and never runs on a pull request. A red run means a limit let too many provider calls through, or `/scan` answered 500: investigate it, do not just re-run.

| File | Role |
|---|---|
| `perf/fake_provider.py` | Stdlib HTTP server with fixed Anthropic Messages replies (two books, fixed token usage), configurable latency and error rate, and an empty Open Library. `GET /__count` returns `calls`, `errors`, `in_flight`, `max_in_flight`; `POST /__reset`, `POST /__config` |
| `perf/locustfile_scan.py` | The three scenarios and their post-run assertions |
| `perf/run_scan_abuse.py` | Runner: resets the database, creates users and a throwaway RS256 key pair, writes `ai_settings`, starts uvicorn and Locust per scenario |
| `perf/perf_setup.py` | The database and token helpers (refuses a non-local database or one not confirmed as throwaway) |
| `requirements-perf.txt` | The app requirements plus Locust, pinned (kept out of `requirements.txt`) |

What each scenario asserts (the real refusal codes: `daily_scan_limit`, `scan_in_progress`, `ai_budget_reached`, `scan_disabled`):

| Scenario | Drive | Assertions |
|---|---|---|
| `abuse` (a) | 4 connections of ONE user, `quota.free.found_daily` = 3 | provider calls >= 1, <= 3 (plus injected errors) and equal to the successful scans (every photo is new); 429 `daily_scan_limit` and 429 `scan_in_progress` seen; exactly 1 provider call at once for the user (the in-flight lock) |
| `breaker` (b) | 8 users, `breaker.free_daily_ceiling_micros` = 6 x the 10,000-micro estimate, a reply that costs 7,000 | 503 `ai_budget_reached` seen; provider calls inside the window the ceiling allows (`ceiling // estimate` .. `(ceiling - estimate) // cost + 1`, here 6..8); calls stop (at least 4 samples over the last 3 s, all equal to the final count, while requests are refused) |
| `kill` (c) | 4 users; `python -m app.ai.settings set scan.standard.enabled false` 8 s in | the provider was called before the switch; every request started more than the cache TTL (2 s) plus a 2 s margin after the CLI returned got 503 `scan_disabled`; the first one came within TTL + margin of the moment the CLI STARTED (the strictest reading, so the margin covers the CLI's start-up); at least 4 independent samples after the TTL show the provider count frozen at the final read |

All scenarios also assert there are no 500s and no response outside the documented set (200 and the refusals above). Every photo is random pixel noise, so scan records (#585) never answer a request: the run measures the quota, not the cache. `scan.standard` is switched to the Anthropic adapter with the real `switch_provider`, and the adapter reaches the fake through `AI_ANTHROPIC_BASE_URL` (only set in the processes the runner starts; see `docs/deployment.md`).

What is different from production, on purpose, and only in the processes the runner starts: `RATE_LIMIT_SCAN=100000/minute` (production's 10/minute per user would throttle one Locust process before any quota is reached; the default is unchanged), `AI_SETTINGS_CACHE_TTL_SECONDS=2`, `scan.requests_per_user_daily` raised to 10,000 so the allowance, not the per-day request count, is what is measured, `TURNSTILE_REQUIRED=false`, `CLAMAV_ENABLED=false` (no clamd in the stack; image normalisation still runs), and `HTTPS_PROXY` aimed at a dead local port so enrichment cannot reach Google Books.

Run it locally (about 75 s for all three scenarios):

```bash
cd backend
pip install -r requirements-perf.txt
createdb bs539_perf && DATABASE_URL=postgresql+asyncpg://localhost/bs539_perf alembic upgrade head   # a THROWAWAY database, its name must contain "perf"
PERF_CONFIRM_ISOLATED_DB=yes DATABASE_URL=postgresql+asyncpg://localhost/bs539_perf \
  python perf/run_scan_abuse.py                      # or --scenario abuse|breaker|kill
```

**The runner wipes data.** For every scenario it deletes ALL rows of `ai_settings` (every operator dial and kill switch, which is why the database must be throwaway), `ai_quota`, `ai_inflight`, `ai_spend_daily`, `ai_usage`, `ai_usage_daily`, `ai_enhanced_zero_results`, `scan_records` and `scan_usage_daily`, plus the `*@perf.invalid` users and only their `refresh_tokens`. It therefore refuses to start unless `DATABASE_URL` parses to a local host (`localhost`, `127.0.0.1`, `::1`), has NO query parameter at all (the driver lets `?database=`, `?host=`, `?user=` and the like override the parsed URL), names a database containing `perf` (the workflow uses `perf`), and `PERF_CONFIRM_ISOLATED_DB=yes` is set. As defence in depth, before any delete and in the same session that deletes, it asks the server for `current_database()` and refuses unless the database it actually reached contains `perf`. (The server address isn't checked: in the workflow, Postgres runs in a service container reached through a published port, so it reports its own bridge address.) It also refuses to start its own fake provider when something already answers on the stub port (a stale one would carry old counters); pass `--external-stub` when the workflow started it. `--tamper` asserts a deliberately wrong cap, ceiling and TTL/margin while the app keeps its real settings. It exits 0 only when every scenario failed on its own target assertion (read from the result file) with Locust's assertion exit code 3 and the health checks (responses recorded, no 500, no unexpected status, sampling) passing; a scenario that crashed counts as "not proven". The API's log for each scenario is in `backend/perf-logs/` (uploaded as an artifact by the workflow). A story that changes a scan limit, the breaker or the kill switch should re-run it.

### AI scan evaluation (#536, on demand)

`backend/eval/ai_scan/` evaluates ONE scan model on the owner's real, labelled shelf photos, to choose the model per tier (#547: Haiku 5.5 for `scan.standard`, Sonnet 5.5 for `scan.enhanced`). It calls a real, paid model, so it is **not a gate**: it runs only on demand from `.github/workflows/ai-eval.yml` (`workflow_dispatch`, inputs `feature`, `model` and `spend_cap_usd`, default $10, in the `ai-eval` GitHub environment), never on a pull request, on a schedule or in a test job. Its unit tests (`tests/unit/test_ai_eval_*.py`, `tests/security/test_ai_eval_privacy.py`) DO run in CI with a fake R2 client and a fake adapter; `backend/eval/` itself is omitted from coverage like `perf/`.

- **Secrets first:** configure the `ai-eval` environment (deployment branches `dev` only, the owner as required reviewer, prevent self-review) BEFORE adding any secret; otherwise anyone with write access could dispatch an edited workflow from another branch and read the keys. Steps in the README.
- **Dataset:** the photos and their ground truth (`manifest.json`: per photo, every book's title, author and optional ISBN) live only in a private, encrypted Cloudflare R2 bucket with an access list, read with a read-only, bucket-scoped token. The location and token come from the `AI_EVAL_R2_*` environment variables (environment secrets in CI), never from the repo. Nothing from the dataset is committed, put in Git LFS or uploaded as an artifact; `tests/unit/test_ai_eval_repo.py` fails if an image or manifest is tracked under `backend/eval/`. Downloads go to a temporary directory removed at the end of the run, also on failure. Layout, validation rules and the owner setup (bucket, token, `ai-eval` environment and its secrets): [`backend/eval/README.md`](../backend/eval/README.md).
- **Path:** each photo goes through `app.ai.images.normalise_image` (as `/scan` does, so token counts match production), then the adapter `app.ai.gateway` builds for the registry spec with the chosen model (registry `max_tokens`, effort, timeout and image size), under the gateway's deadline. No `/scan`, database, quota, breaker or ledger. `tests/security/test_ai_eval_choke_point.py` fails if any file under `backend/eval/` imports a provider SDK or `app.ai.providers`, names a provider host or reads a provider key (the app's choke-point test only scans `backend/app`).
- **Scoring:** deterministic, no LLM judge: an ISBN match, or the app's `titles_agree` plus matching authors (same normalised name or surname), one-to-one (maximum matching, no double counting), after collapsing duplicate ground-truth entries per photo (the adapters drop duplicate predictions, so each title is listed once per photo). Details in the README.
- **Spend cap:** `--spend-cap-usd` is required (at most $100); the runner stops before any call whose upper-bound cost could take the run over the cap, counts errored and interrupted calls at that bound (each bound is reserved before the provider is awaited, then replaced by the real cost when the call is answered), and reports whether the cap stopped it. The bound uses the normalised photo's real dimensions and the model's own image-token formula (Claude `w x h / 750`; OpenAI high-detail tiles, gpt-4o-mini 2,833 + 5,667 per tile) plus a full `max_tokens` reply; a model with no formula is refused. A run that stops early (repeated failures, an R2 or disk error, Ctrl-C) still writes its partial report with `run.aborted`.
- **Report** (`ai-eval-report.json`, the only artifact): feature, provider, model, effort, `max_tokens`, price version; dataset version and counts; `accuracy` (micro precision, recall, F1 over all books, per-image means, matched / predicted / truth counts) and `accuracy_answered_calls` (the same over answered calls only, so provider errors are not read as model accuracy); `outcomes` (counts and rates of ok, refused, truncated, malformed, error, plus `production_malformed_rate`, where a truncated reply with no book counts as malformed as `/scan` treats it); `latency_s` (mean and nearest-rank p95 of answered calls); `tokens` (input, output, `cache_read`, `cache_creation` totals); `cost` (total and per scan, priced with the ledger's `app.ai.prices.cost_micros` from each call's real usage, as there are no ledger rows); `spend_cap` (cap, per-call bound, counted spend, `stopped_by_cap`). It never holds an image, EXIF data, a title, author, case id, object key, bucket or endpoint.
- **Regression rule:** re-run the eval for the affected feature whenever a scan prompt, model or `max_tokens` changes (or the image normalisation), and compare with the last report before switching a tier's model. The decision record goes in the AI gateway docs (#541), not here.

Run locally: `cd backend && pip install -r requirements-eval.txt && python -m eval.ai_scan.run_eval --feature scan.standard --model claude-haiku-5-5 --spend-cap-usd 10` with the `AI_EVAL_R2_*` variables and the provider's API key set. The adapters' own retry on 429 and 5xx applies.

### Writing a new backend test

For an endpoint test (mocks DB + uses ASGI transport):
```python
from unittest.mock import AsyncMock, patch
import pytest
from httpx import ASGITransport, AsyncClient

@pytest.mark.asyncio
async def test_something(app):
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=mock_session)
    cm.__aexit__ = AsyncMock(return_value=False)

    with patch("app.main.AsyncSessionLocal", return_value=cm):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.get("/some-path")

    assert resp.status_code == 200
```

For a security test, place it under `tests/security/` and mark it:
```python
@pytest.mark.security
async def test_sql_injection_on_foo(...):
    ...
```

For an integration test, place it under `tests/integration/` and use `db_session`:
```python
@pytest.mark.integration
async def test_dedup_across_editions(db_session: AsyncSession):
    ...
```

## Frontend (Jest + Expo)

**Location:** `frontend/__tests__/`
**Config:** `frontend/package.json → jest` key (preset `jest-expo`, transform-ignore-patterns)
**Setup:** `frontend/jest.setup.ts` (initializes i18n synchronously, configures RNTL with `concurrentRoot: false` to avoid waitFor races on CI)

### Structure

```
frontend/__tests__/
├── unit/            Screen + hook + context + component unit tests
└── integration/     Cross-provider flows (e.g. ScanCaptureFlow)
```

Locale guards in `unit/`: `localeKeyParity.test.ts` (every `en` key exists in every locale, plural-aware) and `localeCharacterLimits.test.ts` (`_meta` character limits).

### Common mock patterns

**expo-crypto (used by ScanJobContext):**
```typescript
let counter = 0;
jest.mock('expo-crypto', () => ({
  randomUUID: () => `test-uuid-${++counter}`,
}));
```

**expo-file-system:**
```typescript
jest.mock('expo-file-system', () => ({
  Paths: { document: 'file:///docs' },
  Directory: jest.fn().mockImplementation((...args) => ({
    uri: args.join('/'),
    exists: true,
    create: mockDirCreate,
  })),
  File: jest.fn().mockImplementation(...),
}));
```

**Sentry:**
```typescript
jest.mock('../../lib/sentry', () => ({
  Sentry: { captureException: jest.fn(), addBreadcrumb: jest.fn() },
}));
```

**expo-camera:**
```typescript
jest.mock('expo-camera', () => {
  const React = require('react');
  const MockCameraView = React.forwardRef(({ children }, ref) => {
    React.useImperativeHandle(ref, () => ({ takePictureAsync: mockTakePictureAsync }));
    return <>{children}</>;
  });
  return { CameraView: MockCameraView, useCameraPermissions: () => [{ granted: true }, jest.fn()] };
});
```

Integration tests keep the real `BannerProvider` + `InAppBanner` so they exercise the full banner-display path — see `__tests__/integration/ScanCaptureFlow.test.tsx` for the pattern.

### Running

```bash
cd frontend

# Full suite
npx jest

# With coverage
npx jest --coverage

# Single file
npx jest __tests__/unit/useScanJobs.test.tsx

# Type-check (tsc --noEmit; also the typecheck-frontend CI job)
npm run typecheck

# Unused files, exports and dependencies (knip; also the static-frontend CI job)
npm run knip

# Watch mode
npx jest --watch

# Match by name
npx jest -t "renders an error banner"
```

### Writing a new frontend test

Use `@testing-library/react-native` queries:
```typescript
import { render, fireEvent, waitFor } from '@testing-library/react-native';
import MyScreen from '../../app/(tabs)/my-screen';

test('renders and responds to tap', async () => {
  const { getByLabelText } = render(<MyScreen />);
  fireEvent.press(getByLabelText('Do thing'));
  await waitFor(() => expect(getByLabelText('Result')).toBeTruthy());
});
```

**Query priority** (from most-accessible to least):
1. `getByRole`, `getByLabelText` — preferred, matches how screen readers find elements
2. `getByText` — for visible text content
3. `getByTestId` — last resort, needs an explicit `testID` prop

## Web e2e (Playwright, #470)

`frontend/e2e/` is a Playwright suite (Chromium) that drives the **static web export** of the app against a **real backend on a scratch Postgres**. It is not part of Jest (`*.e2e.ts`, outside Jest's test patterns) and is **not a required check**: `.github/workflows/e2e-web.yml` runs it on pull requests to `dev` that touch `frontend/**`, `backend/**` or the workflow, and on demand (`workflow_dispatch`). On failure the HTML report and traces are uploaded as the `e2e-web-report` artifact.

| Scenario (file) | What it checks |
|---|---|
| Auth (`auth.e2e.ts`) | The login screen's dev **Test Login** signs in and lands on My Books |
| Scan upload (`scan.e2e.ts`) | A photo upload shows progress, then the two books the fake provider "reads"; `POST /scan` reaches the fake provider exactly once |
| My Books (`my-books.e2e.ts`) | The library loads, the status and text filters work, a book opens its details |
| Add, delete, restore (`add-and-trash.e2e.ts`) | Search, add, remove (banner), restore from Trash |
| Wishlist (`wishlist.e2e.ts`) | Add from a search, remove |
| Settings (`settings.e2e.ts`) | The scan allowance is shown (the screen has no email today), sign out returns to login |

Every scenario also runs `@axe-core/playwright` with the tags `wcag2a, wcag2aa, wcag21aa, wcag22aa` on each screen it visits (`expectNoA11yViolations` in `e2e/support/fixtures.ts`).

**How it avoids real services**

- **Sign-in:** the web build keeps tokens in memory only (`lib/storage.ts`), so seeding tokens is not possible. The suite clicks the login screen's **Test Login** button, which calls `POST /auth/test-login` (dev only: the route does not exist when hardened, #688). That button exists only when `__DEV__` is true, so the export is a development bundle (`npm run e2e:build` runs `expo export --platform web --dev`) with the test secret and email baked in through `EXPO_PUBLIC_TEST_AUTH_SECRET` / `EXPO_PUBLIC_TEST_EMAIL`. All tests share that one user; each starts by removing its library entries (`Backend.resetLibrary`).
- **AI provider:** `e2e/stack/run_backend.py` starts the API with `AI_ANTHROPIC_BASE_URL` pointing at a fake server (`e2e/stack/fake_services.py`, which reuses `backend/perf/fake_provider.py` from the nightly load run, #539), a dummy `ANTHROPIC_API_KEY`, no OpenAI key, and `scan.standard` switched to the Anthropic adapter. No real model is called and no key is needed. `TURNSTILE_REQUIRED=false`, `CLAMAV_ENABLED=false`.
- **Book catalog (search and enrichment):** the same fake server answers Open Library (`OPEN_LIBRARY_BASE_URL`) and Google Books from a four-book catalog (Dune, The Great Gatsby, Emma, Frankenstein). Google Books has no base-URL setting (the URL is a constant in `app/services/google_books.py`), so the launcher rebinds that constant in its own process; no app code changed. The search-and-add scenarios therefore use the real search, enrichment, catalog-token and `POST /wishlist` code with no public network. `HTTPS_PROXY` is pointed at a dead port in that process, so a call that escapes the fakes fails instead of leaving the machine.
- **Rate limits** are switched off in that process (`/auth/test-login` is 2/minute per IP and every test signs in); limits are covered by the backend tests.
- Saved scan records (#585) answer a repeated photo without calling the provider, so scan tests first call the fake's `POST /__forget_scans`.

**Run it locally** (Node 22, Python 3.12 with `backend/requirements.txt`, a scratch Postgres 16):

```bash
createdb bookshelf_e2e
cd backend && DATABASE_URL=postgresql+asyncpg://localhost/bookshelf_e2e alembic upgrade head
cd ../frontend
npm ci
npx playwright install chromium        # once
npm run e2e:build                       # the web export into dist/ (about a minute)
DATABASE_URL=postgresql+asyncpg://localhost/bookshelf_e2e npm run e2e
```

`playwright.config.ts` starts the backend (`:8001`, fakes on `:18080`) and a static server for `dist/` (`:8081`), and reuses them when they are already running (outside CI). Without a Playwright-managed browser, set `E2E_CHROMIUM_PATH` to a Chromium binary. `npx playwright test e2e/wishlist.e2e.ts --headed` runs one file; `npx playwright show-report` opens the report. Rebuild the export after changing app code. The database must be **throwaway**, and this is enforced (`e2e/stack/db_guard.py`, unit tested by `backend/tests/unit/test_e2e_db_guard.py`): the launcher writes `ai_settings` rows and `/__forget_scans` deletes every scan record, so it exits before the app starts unless `DATABASE_URL` is a local host (`localhost`, `127.0.0.1`, `::1`) with no query parameters and a database name containing `e2e` (CI uses `e2e`), `ENVIRONMENT` is not staging or production, and the URL does not look hosted (`supabase`, `render.com`, `pooler`). `E2E_ALLOW_DB=<exact database name>` allows one other local name; it never lifts the host, query, hosted or environment checks. The connected `current_database()` is checked again before the first write, and `/__forget_scans` refuses to run unless the guard passed.

**Known app defects the suite carries** (each is a real finding, kept visible rather than hidden):

- *Scan upload on web fails.* `ScanJobContext` appends the photo's blob URL string to the form data, which browsers reject (`parameter 2 is not of type 'Blob'`), so no `POST /scan` is sent. The scan UI test is marked `test.fail(...)`: it passes as an expected failure now and turns red once the upload works, which is the cue to remove that line (with the upload fixed it passes as written).
- *Accessibility.* A scenario tolerates a violation only through `KNOWN_VIOLATIONS` in `e2e/support/fixtures.ts`: one rule on the matching nodes, every other rule still runs, and the call fails when axe stops reporting it (so a fix has to remove the entry). Never add one without an issue. Today: book cards that are buttons containing buttons (`nested-interactive`), picker rows with `role="checkbox"` and no `aria-checked`, the wishlist cover placeholder's `aria-label` on a role-less div, the success banner's white-on-green contrast (3.29:1), and the scan spinner as an unnamed progressbar.
- The development bundle's LogBox toast (`#error-toast`) covers the tab bar, so the fixtures hide it; it is not app UI.

## CI test jobs

Most jobs are reusable workflows from `wcmchenry3-stack/.github`; `typecheck-frontend`, `js-bundle-check` and the other bundle/build checks are defined inline in `ci.yml`:

| Job | What it runs |
|---|---|
| `test-python` | `pytest` with coverage (default, no integration) |
| `test-frontend` | `npx jest --coverage`; fails below the Jest `coverageThreshold` floor (see Coverage targets) |
| `test-migrations` | Against a Postgres 16 service: `alembic upgrade head`, `alembic check`, a `downgrade -1` smoke test, then `pytest -m integration tests -q -rs` with `REQUIRE_DB_TESTS=1` (every integration-marked test in every folder, on real Postgres) |
| `pytest -m security` (`security.yml`, job `pytest-security`) | Separate run of the security marker (security tests also run in `test-python` on every PR). Only on PRs to `dev` that touch the paths listed in "When the security suite runs", plus weekly (Monday 08:00 UTC) and manual dispatch |
| `lint-python` | `black --check` + `ruff` |
| `lint-frontend` | `eslint` + `prettier --check` |
| `typecheck-frontend` | `npm run typecheck` (`tsc --noEmit`), covers tests too |
| `cve-python` | `pip-audit` |
| `cve-frontend` | `npm audit` |
| `sast-python` | Bandit static analysis |
| `secret-scan` | gitleaks |
| `detect-changes` | Always runs. Diffs the PR against its base and outputs `native` and `frontend` (see "Conditional bundle and native build checks" below) |
| `js-bundle-check` | Metro bundle compiles for iOS. Runs only when `frontend` is true |
| `android-bundle-check` | Metro bundle compiles for Android. Runs only when `frontend` is true |
| `ios-build-check` | Full `xcodebuild archive` (no signing). Runs only when `native` is true and the PR is not a draft |
| `android-build-check` | Full `./gradlew assembleDebug`. Runs only when `native` is true and the PR is not a draft |
| `native-build-gate` | Always runs; the required check standing in for the four conditional jobs above (passes if each succeeded or was skipped) |
| `repo-hygiene` | Own workflow (`repo-hygiene.yml`): jscpd copy-paste threshold, 600-line file cap, backend dead-code check (vulture), offline docs link and anchor check. A weekly `external-links` job checks external URLs (not a PR gate). See "Repo hygiene checks" below |
| `static-frontend` | Own workflow (`static-frontend.yml`; every PR to `dev`/`main`, no paths filter so it can be required, and pushes to `dev`): `npm ci` then `npm run knip` (#768, #771) |
| `design-token-check` | Own workflow (`design-token-check.yml`, calls the shared `called-design-token-check.yml`): the colour-literal gate for CLAUDE.md Hard Rule 5 and the accessibility check. Runs on every PR to `dev`/`main` (opened, edited, synchronize, reopened), so it covers all frontend paths. It stays a separate workflow; do not fold it into `static-frontend.yml` |
| `migration-guard` | Own workflow (`migration-guard.yml`, re-runs on label changes): `scripts/migration_guard/check_destructive_migrations.py` over the migrations a PR adds or changes; fails on a destructive `upgrade()` step unless the PR has the owner-applied label `destructive-migration-approved` (see "Migrations: expand/contract" in `docs/claude/backend.md`). Listed in the required checks of `.github/rulesets/protect-main-dev.json`; the owner must apply the ruleset in GitHub settings. On PRs into `main` it passes without scanning (already scanned on the feature PRs into `dev`) |

### Composite setup actions (#770)

Jobs that set up Node or Python use two local composite actions instead of repeating `setup-node`/`setup-python` steps. Pins and cache settings live only in these files. Each one needs `actions/checkout` first.

- `.github/actions/setup-frontend/action.yml`: `actions/setup-node` with the npm cache (`frontend/package-lock.json`), then `npm ci` in `frontend/`. Inputs: `node-version` (default `'22'`; pass `'20'` only where a job pins 20, as `npm audit` in `security.yml` does) and `install` (default `'true'`; `'false'` sets up Node and the cache only).
- `.github/actions/setup-backend/action.yml`: `actions/setup-python` (`python-version` default `'3.12'`) with the pip cache (`backend/requirements*.txt`), then `pip install -r` for each file in `requirements`. Inputs: `requirements` (space-separated, relative to `backend/`, default `'requirements.txt'`; `''` installs nothing) and `python-version`.

### Conditional bundle and native build checks

The four slowest jobs do not run on every PR. `detect-changes` (plain `git diff --name-only <base>...HEAD`, no third-party action) decides:

| Output | True when the PR touches | Gates |
|---|---|---|
| `frontend` | `frontend/**`, `.github/workflows/ci.yml` or `.github/actions/setup-frontend/**` | `js-bundle-check`, `android-bundle-check` |
| `native` | `frontend/ios/**` (Podfile, `ci_scripts/`), `frontend/android/**` (Gradle files), `frontend/plugins/**`, `frontend/app.json`, `frontend/app.config.*`, `frontend/package.json`, `frontend/package-lock.json`, or `.github/workflows/ci.yml` | `ios-build-check`, `android-build-check` |

- On `push` (dev/main) and `workflow_call` (Deploy) there is no PR base, so both outputs are `true` and everything always runs.
- **Draft PRs skip only `ios-build-check` and `android-build-check`.** Every other job, including the bundle checks, still runs on drafts. `ready_for_review` is a `pull_request` trigger type, so the native builds run when the PR is marked ready. Because of the `cancel-in-progress` concurrency group, that run cancels any in-flight draft run, which is expected.
- A job skipped by `if:` reports as success to required checks, so a skipped build check does not block merge. A docs-only or backend-only PR therefore shows these jobs as skipped.
- The required check is `native-build-gate` (always runs, `if: always()`), not `ios-build-check / Xcode Build (no signing)`. A reusable-workflow job skipped by `if:` reports under the plain job name (`ios-build-check`, skipped), never under the `<job> / <inner job>` name, so the inner name would never report on a skipped PR and block merge. The gate passes when `ios-build-check`, `android-build-check`, `js-bundle-check` and `android-bundle-check` each succeeded or were skipped, and fails on failure or cancellation, or if `detect-changes` did not succeed.
- `podfile-lock-check` and `gradle-wrapper-check` are unchanged.

### Required status checks (#771)

The required checks live in `.github/rulesets/protect-main-dev.json` (the owner applies the ruleset in GitHub settings by hand). A context must be exactly what GitHub reports: an in-repo job is its `name:` (else its job id); a reusable-workflow job is `<caller job id> / <inner job name>`.

- A job skipped by `if:` still reports (as skipped) and passes. A workflow that does not run at all because of `on.pull_request.paths` never reports, so it blocks every PR outside those paths: never require a path-filtered workflow unless its filter is removed. This is why `static-frontend.yml` has no `paths:` filter. `e2e-web`, everything in `security.yml`, `doc-reminder`, `external-links`, `OWASP ZAP Active Scan` and `detect-changes` are deliberately not required.
- `ios-build-check` and `android-build-check` are never required directly: when they run they report `ios-build-check / Xcode Build (no signing)` and `android-build-check / Gradle Build (Debug)`, when skipped the plain ids, so no single name always reports. `native-build-gate` is the required stand-in for both (and for the bundle checks); this is how android-build-check is covered.
- `backend/tests/unit/test_ruleset_required_checks.py` (run by `test-python`) fails when a required context matches no workflow job, when its workflow has a `paths`/`paths-ignore` filter or does not run on PRs into `dev`, or when a native build job is required directly.

### Fixing or ignoring a knip finding (#768)

`frontend/knip.json` configures knip: entry points are `app/**` (Expo Router), `__tests__/**` (Jest), `plugins/*.js`, `scripts/*.js` and `components/*.web.tsx` (platform files); the Jest, Babel and Expo plugins add `jest.setup.ts`, `babel.config.js` and the `app.json` plugin references.

- Unused file, export or type: delete it, or drop the `export` keyword if the symbol is still used in its own file. A test counts as a user, so an export used only by tests is not reported.
- Unused or unlisted dependency: remove it from `package.json`, or add it to `package.json` if it is really imported.
- False positive (read at runtime, a native or config-only reference, a dynamic `require`): add it to `ignore`, `ignoreDependencies`, `ignoreUnresolved` or `ignoreIssues` in `knip.json` with a `//` comment saying why. No blanket globs.

Required checks are listed in `.github/rulesets/protect-main-dev.json` by the exact name GitHub reports. A job that calls a reusable workflow reports as `<job> / <inner job name>` (e.g. `secret-scan / Secret scan (gitleaks)`), a plain job by its id (e.g. `test-migrations`). Copy names from a PR's checks list; a name that never reports blocks every merge. The file is a record, not applied automatically: mirror any change in Settings → Rules → Rulesets.

Not a required check: `.github/workflows/e2e-web.yml` runs the Playwright web e2e suite (see "Web e2e (Playwright, #470)") on pull requests to `dev` that touch the frontend, the backend or the workflow.

Not part of the PR checks: `.github/workflows/perf-ai.yml` (nightly `schedule` and `workflow_dispatch`, not a required check) runs the scan abuse load run described under "Nightly scan abuse load run" above, and `.github/workflows/ai-eval.yml` (`workflow_dispatch` only) runs the AI scan evaluation described under "AI scan evaluation" above.

## Repo hygiene checks

`.github/workflows/repo-hygiene.yml` (job `repo-hygiene`, on every PR and push to `dev`/`main`) runs four checks (#769, and the vulture check from #813). It is a separate workflow from `ci.yml`. The tools run by exact version (`JSCPD_VERSION`, `LYCHEE_VERSION` in the workflow), not through third-party actions; the lychee tarball is checked against a pinned sha256 (`LYCHEE_SHA256`). Bump the version and the checksum together.

- **Copy-paste detection (jscpd).** `.jscpd.json` at the repo root scans `backend/app` and `frontend/{app,components,contexts,hooks,lib}` (tests and locales excluded). The `threshold` is a percentage of duplicated lines, set just above the measured level (1.42% when added, threshold 1.6). New duplication that pushes the total over it fails the job. The JSON report is uploaded as the `jscpd-report` artifact. To run it locally: `npx jscpd@5.4.0 --config .jscpd.json` from the repo root; the report lands in `jscpd-report/` (gitignored). When you remove duplication, lower `threshold` to just above the new measured percentage so the gain is kept. Never raise it to fit new copy-pasted code; extract the shared code instead.
- **File-length cap.** `python3 scripts/check_file_length.py` fails when a `.py`, `.ts` or `.tsx` file is over 600 lines. Tests, `__tests__`, locales, generated files, `backend/alembic/versions`, `node_modules`, `ios/` and `android/` are not counted. Files that were already over the cap are in `scripts/file_length_allowlist.txt` (`path max_lines`), each capped at its length when it was listed: it can shrink but not grow. When you shrink one, lower its number (the script warns about stale entries; `--strict` makes that an error); delete the line once the file is under 600. Do not add a file to the allowlist to make a new long file pass; split it. Tested by `backend/tests/unit/test_check_file_length.py`.
- **Backend dead code (vulture, #813).** From `backend/`: `vulture app --min-confidence 60` (the CI command, after `pip install -r requirements-dev.txt`, which pins `vulture==`). Config is `[tool.vulture]` in `backend/pyproject.toml`: `ignore_decorators` covers route handlers (`@router.*`, `@app.*`) and pydantic validators and serializers, and `ignore_names` is the whitelist. Vulture matches a name everywhere in `app/`, so keep each entry specific. An entry is for a name used at runtime that static analysis cannot see (a pydantic response or request field, an ORM column, a settings key read by `getattr`, a framework hook), and each one needs a one-line comment saying how it is used. Do not whitelist to get a PR through: delete the code, and its tests if they were its only callers (with owner sign-off), or fix the caller. When you delete code vulture flagged, remove its whitelist entry in the same change. Entries marked "PENDING" are pending owner decision #830 (tested-only functions and unread settings); do not add to that group. CI installs only the vulture pin into a venv, not the backend's runtime deps, because vulture reads the AST.
- **Docs links (lychee).** `lychee.toml` at the repo root. On PRs: `lychee --config lychee.toml --offline --include-fragments 'docs/**/*.md' README.md CLAUDE.md`, i.e. relative links and `#anchors` only, no network. External URLs are checked only by the `external-links` job, which runs weekly (Monday 06:17 UTC) and on manual dispatch and is not a PR gate. A link that cannot resolve in CI on purpose (for example the owner's `~/.claude/standards/` files) goes in `exclude` in `lychee.toml`, with a comment.

## Coverage targets

- **Backend:** 98% line coverage of app code (`pytest --cov=. --cov-fail-under=98`). `backend/.coveragerc` omits `tests/`, `alembic/`, `perf/` (the load-run scripts, #539; their parts the run depends on are unit tested) and `eval/` (the on-demand AI scan evaluation, #536; unit tested by `tests/unit/test_ai_eval_*.py`) and sets `precision = 2`, so the gate compares the total to two decimal places, leaving a rounding window under 0.005 points (pytest-cov rounds to the configured precision, which defaulted to 0 before #621). `tests/unit/test_coverage_gate.py` guards this.
- **Frontend:** `npx jest --coverage` (`npm run test:ci`) enforces `coverageThreshold.global` from the `jest` key in `frontend/package.json` (statements 95, branches 90, functions 92, lines 97, #764), so it fails locally as well as in CI. `collectCoverageFrom` is explicit (app, components, contexts, hooks, lib, src/i18n, theme). The CI `coverage-threshold: 80` input in `.github/workflows/ci.yml` must stay at or below this floor.
  - **Ratchet:** the Jest `coverageThreshold` is the floor and it only goes up. Any PR that raises a metric by 1 point or more may bump that floor (rounded down to the whole percent). Never lower it without an owner decision.

## End-to-end

- **Web e2e:** Playwright against the web export runs in CI (`e2e-web.yml`, not a required check). See "Web e2e (Playwright, #470)" above.
- **Native flows:** Maestro (Phase 4) is planned. The Maestro flows under `frontend/.maestro/` are not run by any workflow yet.

## Rule + memory references

- **Rule #4:** Run tests before marking any task done. The feedback memory "run pre-submit checks before pushing" codifies this — always `pytest --cov` + `eslint` + `jest` + `prettier` locally before opening a PR.
- **Feedback memory:** "integration tests must hit a real database, not mocks" — reason: prior incident where mock/prod divergence masked a broken migration.
