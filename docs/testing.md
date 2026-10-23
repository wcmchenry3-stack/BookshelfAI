# Testing

How Bookshelf is tested: the split between unit / integration / security / accuracy / performance, conftest patterns, and how to run + write new tests. For the Claude-specific standards file (coverage thresholds, rollback fixture conventions) see [`claude/testing.md`](claude/testing.md).

## Philosophy

- **Unit tests mock the DB** — they're fast and run on every PR.
- **Integration tests hit a real Postgres** and are auto-skipped when `DATABASE_URL` points at the dummy URL used by CI's default config. CI runs them in the `test-migrations` job, which has a Postgres 16 service.
- **Security tests are a separate marker** — `pytest -m security` — so the OWASP-aligned suite can be run standalone and surfaced in its own CI job.
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

### Running

```bash
cd backend && source .venv/bin/activate

# Full suite (integration auto-skipped without a real DB)
pytest

# With coverage report
pytest --cov=app --cov-report=term-missing

# Enforce the CI threshold (app code only via backend/.coveragerc, compared to 2 decimal places; #621)
pytest --cov=. --cov-fail-under=90

# Single file
pytest tests/unit/test_health_host_exemption.py

# Single test
pytest tests/unit/test_health_host_exemption.py::test_health_accepts_untrusted_host_header

# Security suite only
pytest -m security

# Integration tests (against a real DB)
DATABASE_URL=postgresql+asyncpg://localhost/bookshelf_test pytest -m integration
```

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

**The runner wipes data.** For every scenario it deletes ALL rows of `ai_settings` (every operator dial and kill switch, which is why the database must be throwaway), `ai_quota`, `ai_inflight`, `ai_spend_daily`, `ai_usage`, `ai_usage_daily`, `ai_enhanced_zero_results`, `scan_records` and `scan_usage_daily`, plus the `*@perf.invalid` users and only their `refresh_tokens`. It therefore refuses to start unless `DATABASE_URL` parses to a local host (`localhost`, `127.0.0.1`, `::1`) with no `host` / `hostaddr` / `service` query parameter, names a database containing `perf` (the workflow uses `perf`), and `PERF_CONFIRM_ISOLATED_DB=yes` is set. It also refuses to start its own fake provider when something already answers on the stub port (a stale one would carry old counters); pass `--external-stub` when the workflow started it. `--tamper` asserts a deliberately wrong cap, ceiling and TTL/margin while the app keeps its real settings. It exits 0 only when every scenario failed on its own target assertion (read from the result file) with Locust's assertion exit code 3 and the health checks (responses recorded, no 500, no unexpected status, sampling) passing; a scenario that crashed counts as "not proven". The API's log for each scenario is in `backend/perf-logs/` (uploaded as an artifact by the workflow). A story that changes a scan limit, the breaker or the kill switch should re-run it.

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

## CI test jobs

Each job is a reusable workflow from `wcmchenry3-stack/.github`:

| Job | What it runs |
|---|---|
| `test-python` | `pytest` with coverage (default, no integration) |
| `test-frontend` | `npx jest` with coverage |
| `test-migrations` | Against a Postgres 16 service: `alembic upgrade head`, `alembic check`, a `downgrade -1` smoke test, then `pytest -m integration tests/integration` (the real-DB integration suite) |
| `pytest -m security` | Separate security-suite run (on every PR) |
| `lint-python` | `black --check` + `ruff` |
| `lint-frontend` | `eslint` + `prettier --check` |
| `cve-python` | `pip-audit` |
| `cve-frontend` | `npm audit` |
| `sast-python` | Bandit static analysis |
| `secret-scan` | gitleaks |
| `js-bundle-check` | Metro bundle compiles for iOS |
| `android-bundle-check` | Metro bundle compiles for Android |
| `ios-build-check` | Full `xcodebuild archive` (no signing) |
| `android-build-check` | Full `./gradlew assembleDebug` |

Not part of the PR checks: `.github/workflows/perf-ai.yml` (nightly `schedule` and `workflow_dispatch`, not a required check) runs the scan abuse load run described under "Nightly scan abuse load run" above.

## Coverage targets

- **Backend:** 90% line coverage of app code (`pytest --cov=. --cov-fail-under=90`). `backend/.coveragerc` omits `tests/`, `alembic/` and `perf/` (the load-run scripts, #539; their parts the run depends on are unit tested) and sets `precision = 2`, so the gate compares the total to two decimal places, leaving a rounding window under 0.005 points (pytest-cov rounds to the configured precision, which defaulted to 0 before #621). `tests/unit/test_coverage_gate.py` guards this.
- **Frontend:** tracked via `npx jest --coverage`, no hard threshold enforced by default

## Rule + memory references

- **Rule #4:** Run tests before marking any task done. The feedback memory "run pre-submit checks before pushing" codifies this — always `pytest --cov` + `eslint` + `jest` + `prettier` locally before opening a PR.
- **Feedback memory:** "integration tests must hit a real database, not mocks" — reason: prior incident where mock/prod divergence masked a broken migration.
