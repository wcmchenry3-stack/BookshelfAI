# Testing Standards

See [~/.claude/standards/testing.md](~/.claude/standards/testing.md) for universal conventions including 80% coverage threshold, rollback fixture pattern, and accessible query priority.

## book_app specific
- Backend: `pytest --cov=. --cov-fail-under=98` from `backend/` (what CI's `test-python` job runs). The backend gate is 98%, not the universal 80%.
  - `backend/.coveragerc` omits `tests/`, `alembic/`, `perf/` and `eval/`, so only `app/` is measured.
  - It also sets `precision = 2`. pytest-cov rounds the total to that precision before comparing it with `--cov-fail-under`, and the default of 0 let 79.7% pass an 80% gate with exit 0 (#621). `tests/unit/test_coverage_gate.py` guards this.
- Frontend: `npx jest --coverage` from `frontend/` (what CI's `test-frontend` job runs). The gate is the Jest `coverageThreshold` in `frontend/package.json` (95/90/92/97, #764), which only goes up. Also `npm run typecheck` (`tsc --noEmit`, the `typecheck-frontend` CI job).
- E2E: web e2e runs in CI (Playwright, below). Native Maestro flows (Phase 4) are planned; `frontend/.maestro/` holds flows but no workflow runs them.
- Web E2E (Playwright, #470): `frontend/e2e/` against the static web export, real backend, fake AI provider/catalog, axe WCAG 2.2 AA on every scenario; `npm run e2e:build && npm run e2e` from `frontend/`; `e2e-web.yml`, not a required check. See `docs/testing.md` "Web e2e (Playwright, #470)"
- Integration tests (`@pytest.mark.integration`, any folder) run only in CI's `test-migrations` job: `pytest -m integration tests -q -rs` with `REQUIRE_DB_TESTS=1`, which errors instead of skipping when there is no real `DATABASE_URL` (#760); see `docs/testing.md`.
- DB isolation: rollback fixture in `backend/tests/conftest.py` — do NOT mock the database
- Cost guard: no test may call a real AI provider. `backend/tests/_network_guard.py` (installed for every run by `backend/tests/conftest.py`) blocks any request to an Anthropic or OpenAI API host and fails the test. Fake providers at the gateway boundary (`backend/tests/limits/_harness.py`) or with `backend/tests/_claude_fake.py`.
- AI scan evaluation (`backend/eval/ai_scan/`, #536): runs a real model only from the on-demand `ai-eval.yml` workflow, never in tests. Its tests use `backend/tests/_ai_eval_fakes.py` (fake R2 client, scripted adapter). Re-run the eval when a scan prompt, model or `max_tokens` changes; see `docs/testing.md`.
- Repo hygiene (#769, `.github/workflows/repo-hygiene.yml`): jscpd duplication threshold (`.jscpd.json`), 600-line source file cap (`scripts/check_file_length.py`, allowlist `scripts/file_length_allowlist.txt` can shrink but not grow), and offline docs link/anchor check (`lychee.toml`). Do not raise a threshold or extend the allowlist to get a PR through; see "Repo hygiene checks" in `docs/testing.md`.

## What runs, and when

Taken from `.github/workflows/` (`ci.yml`, `security.yml`, `migration-guard.yml`, `perf-ai.yml`, `ai-eval.yml`). The full CI job table is in [`docs/testing.md`](../testing.md#ci-test-jobs).

| Check | Where | When |
|---|---|---|
| Backend unit tests, `pytest --cov=. --cov-fail-under=90` (app code only) | `ci.yml` `test-python` | every PR to `dev` or `main` |
| Backend security suite, `pytest -m security` | runs inside `test-python` on every PR (no marker filter in `pytest.ini`); also on its own in `security.yml` `pytest-security` | separate run: PRs to `dev` that touch `backend/app/**`, `backend/tests/security/**`, `backend/requirements*.txt`, `security.yml` or `.zap/**`; weekly (Monday 08:00 UTC); manual |
| Backend integration, `pytest -m integration tests -q -rs` (every folder, `REQUIRE_DB_TESTS=1`) on real Postgres 16 | `ci.yml` `test-migrations`, after `alembic upgrade head`, `alembic check` and `downgrade -1` | every PR to `dev` or `main` |
| Scan limits matrix (`backend/tests/limits/`) | Its `integration`-marked files run in `test-migrations`. `test_no_real_provider.py` has no marker and runs in `test-python`. | every PR to `dev` or `main` |
| Frontend Jest + coverage, `npx jest --coverage` | `ci.yml` `test-frontend` | every PR to `dev` or `main` |
| Type check, `npm run typecheck` (`tsc --noEmit`, covers tests too) | `ci.yml` `typecheck-frontend` | every PR to `dev` or `main` |
| Destructive-migration guard | `migration-guard.yml` | PRs to `dev` or `main`, on open, sync, reopen and label changes |
| Nightly scan abuse load run (Locust against a fake provider) | `perf-ai.yml` | nightly at 03:17 UTC and manual; not a PR check |
| AI scan evaluation (a real, paid model on labelled photos) | `ai-eval.yml` | manual only (`workflow_dispatch`); never a gate |

## Scan limits matrix

`backend/tests/limits/` holds one table of every scan outcome (`test_limits_matrix.py`, `CASES`): for each tier and scan type, the starting counters and `ai_settings` overrides, what the provider does, and then the HTTP status and code, whether the provider was reached, how each counter moves (daily, i.e. found, and no-match since #577, global, zero-result rows; the legacy month rows must never move, since there is no monthly limit, #582) and what the day's spend settles. Next to it: limit changes through the `ai_settings` CLI, parallel requests and lost commit acknowledgements, UTC day boundaries, a month boundary that changes nothing, and many days of use at the daily limit that are never refused for a monthly reason. It runs on real Postgres (`integration`), in CI's `test-migrations` job on every PR.

- **Any story that adds or changes a scan limit must add its rows to `backend/tests/limits/`** (a refusal, a counter, a window, a tier rule, a backstop). Add `Case(...)` rows to `CASES`; if the rule needs a new input or expectation, add a field with a default that leaves the existing rows unchanged. A changed rule changes its rows in the same PR, with a Test changes row citing the AC.
- **Repeats (scan records, #585)** need two uploads of the same photo, so they have their own table in the same file (`REPEATS`): the first upload runs for real, the conditions may change (settings, tier, a lease, the record's age), then the same photo comes back and the second upload is asserted like any row (plus `repeat: true` and one `cache_hit` usage row costing 0). The harness gives every scan its own photo (`_harness.distinct_jpeg()`), so a test repeats a photo only by passing the same `image=` bytes to `call_scan` twice.
- ship-story's test-integrity audit checks this: a PR that touches scan limits (`backend/app/ai/quota/`, `breaker.py`, the limit keys in `settings.py`, the refusals in `backend/app/api/scan.py`) without matrix rows is sent back.
- The drift test of the limits overview against the settings registry is `backend/tests/unit/test_scan_limits_doc.py` (#581): every `ai_settings` key and every `rate_limit_*` setting must be mentioned in `docs/scan-limits.md`. A story that adds a limit adds its row there.
- Matrix rows replace the older per-feature tests (`tests/unit/test_ai_quota.py`, `tests/integration/test_*quota*_db.py`, ...) only when they cover the same thing: the same inputs, the same code path and every assertion the old test makes. Otherwise keep the old test.

## Conditional CI jobs

`js-bundle-check` and `android-bundle-check` run only when the PR changes `frontend/**` or `ci.yml`; `ios-build-check` and `android-build-check` run only when it changes `frontend/ios/**`, `frontend/android/**`, `frontend/plugins/**`, `app.json`/`app.config.*`, `package.json`, `package-lock.json` or `ci.yml` (job `detect-changes`), and never on draft PRs. Push and `workflow_call` runs always run them. The required check is `native-build-gate`, which always runs and passes when each conditional job succeeded or was skipped; a reusable-workflow job skipped by `if:` reports under its plain name, not `<job> / <inner job>`, so the inner name cannot be required. Details: "Conditional bundle and native build checks" in `docs/testing.md`.
- **Frontend static checks (#768):** `cd frontend && npm run knip` (unused files, exports, dependencies) runs in the `static-frontend` workflow. Fix the finding or add a commented entry to `frontend/knip.json`; see "Fixing or ignoring a knip finding" in `docs/testing.md`. The colour-literal gate stays in `design-token-check.yml`.
