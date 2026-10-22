# Testing Standards

See [~/.claude/standards/testing.md](~/.claude/standards/testing.md) for universal conventions including 80% coverage threshold, rollback fixture pattern, and accessible query priority.

## book_app specific
- Backend: `pytest tests/ --cov=app --cov-fail-under=80`
- Frontend: `npx jest --coverage`
- E2E (Detox): runs on PRs to `main` only
- DB isolation: rollback fixture in `backend/tests/conftest.py` — do NOT mock the database
- Cost guard: no test may call a real AI provider. `backend/tests/_network_guard.py` (installed for every run by `backend/tests/conftest.py`) blocks any request to an Anthropic or OpenAI API host and fails the test. Fake providers at the gateway boundary (`backend/tests/limits/_harness.py`) or with `backend/tests/_claude_fake.py`.

## Scan limits matrix

`backend/tests/limits/` holds one table of every scan outcome (`test_limits_matrix.py`, `CASES`): for each tier and scan type, the starting counters and `ai_settings` overrides, what the provider does, and then the HTTP status and code, whether the provider was reached, how each counter moves (daily, monthly, enhanced monthly, global, zero-result rows) and what the day's spend settles. Next to it: limit changes through the `ai_settings` CLI, parallel requests and lost commit acknowledgements, and UTC day and month boundaries. It runs on real Postgres (`integration`), in CI's integration job on every PR.

- **Any story that adds or changes a scan limit must add its rows to `backend/tests/limits/`** (a refusal, a counter, a window, a tier rule, a backstop). Add `Case(...)` rows to `CASES`; if the rule needs a new input or expectation, add a field with a default that leaves the existing rows unchanged. A changed rule changes its rows in the same PR, with a Test changes row citing the AC.
- **Repeats (scan records, #585)** need two uploads of the same photo, so they have their own table in the same file (`REPEATS`): the first upload runs for real, the conditions may change (settings, tier, a lease, the record's age), then the same photo comes back and the second upload is asserted like any row (plus `repeat: true` and one `cache_hit` usage row costing 0). The harness gives every scan its own photo (`_harness.distinct_jpeg()`), so a test repeats a photo only by passing the same `image=` bytes to `call_scan` twice.
- ship-story's test-integrity audit checks this: a PR that touches scan limits (`backend/app/ai/quota.py`, `breaker.py`, the limit keys in `settings.py`, the refusals in `backend/app/api/scan.py`) without matrix rows is sent back.
- The drift test of the limits overview against the settings registry is not here: #581 adds it.
- Matrix rows replace the older per-feature tests (`tests/unit/test_ai_quota.py`, `tests/integration/test_*quota*_db.py`, ...) only when they cover the same thing: the same inputs, the same code path and every assertion the old test makes. Otherwise keep the old test.
