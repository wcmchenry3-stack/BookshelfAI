# Test integrity

The rule in one line: **a failing test is evidence about the code until proven
otherwise.** Changing a test is allowed when the _intended behaviour_ changed,
never to make _unintended behaviour_ pass.

## What counts as "existing" vs "new"

- **New test**: added in this PR. You may change, rewrite or delete it freely
  while the PR is open, including fixing your own flaky new test.
- **Existing test**: present on `origin/dev` when the branch was cut. This
  policy governs changes to these.

## Decision table for existing tests

| Situation                                                                                                        | Allowed?                        | What to do                                                                                                                                                                                           |
| ---------------------------------------------------------------------------------------------------------------- | ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| The story intentionally changes the behaviour the test asserts (an AC says so, or it follows directly from one)  | Yes: **update** the assertion   | Change only what the AC changes. Add a row to **Test changes** citing the AC.                                                                                                                        |
| The story removes the feature the test covers                                                                    | Yes: **delete**                 | Add a row citing the AC that removes the feature.                                                                                                                                                    |
| The test breaks because of a refactor that should preserve behaviour (renamed export, moved file, new signature) | Yes: **mechanical update** only | Update imports, names, fixture shape. The expected _values_ must not change. Add a row marked "mechanical".                                                                                          |
| The test fails and no AC explains the behaviour change                                                           | **No**                          | Treat it as a bug in this PR and fix the code. If you believe the old expectation is wrong, that's an owner decision (see escalation.md); don't edit the test.                                       |
| It's unclear whether the behaviour change is intended                                                            | **No**, until decided           | Open an owner decision issue. Work on other stories meanwhile.                                                                                                                                       |
| The test is flaky and unrelated to this PR's code                                                                | **No** changes in this PR       | Re-run once to confirm. File a `bug` issue with the failing run link and the output, and reference it on the PR. Don't skip it, retry-wrap it or delete it.                                          |
| The test is flaky and this PR touched the code or test it covers                                                 | Yes: fix the **cause**          | Make the test deterministic: fake timers, frozen time, awaited state, stable selectors, isolated DB state. Longer timeouts or retries as the only change are not allowed. Add a row to Test changes. |

## Always treated as weakening (needs an explicit owner-approved reason)

- **Skip/only markers:** `.skip`, `xit`, `xdescribe`, `it.only` or
  `describe.only` left in, `test.fixme`; adding `@pytest.mark.skip` or
  `skipif`, `xfail`, or `pytest.importorskip`.
- **Deselecting tests in CI or local commands:** `-m "not security"`, `-k`
  exclusions, `--deselect`, or removing a marker so a test drops out of a
  selected suite.
- **Weaker assertions:** removing an `expect` or `assert`, or swapping a
  precise matcher for a looser one (`toEqual` → `toBeDefined`/`toBeTruthy`, an
  exact status code → `in (200, 4xx)`, an exact number → `>`).
- **Snapshots:** updating them (`-u`) without saying which snapshots changed
  and why each one did.
- **Relaxed thresholds:** raising tolerances, timeouts or retry counts;
  lowering coverage thresholds (backend `coverage-threshold: 80` in
  `ci.yml`, or any frontend `coverageThreshold`); relaxing the accuracy
  tests' thresholds (`backend/tests/accuracy/`).
- **Making a security test pass by changing what it attacks:** editing a
  payload, an expected rejection, or a rate-limit count in
  `backend/tests/security/`.
- **Changing the fixtures, not the code:** editing fixtures, golden files or
  mocked provider responses so they match new output.
- **Ignore lists:** adding paths to test, coverage or lint ignore lists.
- **Hiding integration tests:** making them skip by default, or removing the
  Postgres requirement.

If one of these is genuinely right for an AC-driven change (e.g. a security
test whose endpoint the story deliberately removes), it is allowed, with a
Test changes row that names the AC. Without an AC, it needs an owner decision.

## The Test changes table (required in every PR body)

```
## Test changes
| Existing test | Change | Why (AC / mechanical / flaky-fix) |
| --- | --- | --- |
| backend/tests/unit/test_wishlist.py › test_creates_book_from_client_fields | now asserts catalog row is NOT created from client fields | AC1: shared catalog written only from provider data |
```

Write `None — only new tests added.` when no existing test was touched.

## Audit (orchestrator, Phase 3, and after every later push that touches tests)

1. List existing test files the PR changed or deleted:
   ```bash
   git diff --name-status origin/dev...HEAD -- \
     'frontend/__tests__/**' 'frontend/**/*.test.*' 'backend/tests/**' 'backend/pytest.ini' '.github/workflows/**'
   ```
   `M`, `D` and `R` entries are existing tests; `A` entries are new. Workflow
   and `pytest.ini` changes count because they can deselect tests.
2. Grep the diff for the weakening patterns above:
   ```bash
   git diff origin/dev...HEAD | grep -nE '^\+.*(\.skip\(|\bxit\(|\bxdescribe\(|\.only\(|test\.fixme|mark\.skip|xfail|importorskip|--deselect|-m "not |coverage-threshold|coverageThreshold)'
   ```
   Then read the `-` lines of each modified existing test for removed asserts
   or loosened matchers.
3. Every modified or deleted existing test, and every weakening hit, must have
   a matching Test changes row with a valid reason. A missing row or an
   invalid reason is a blocking finding; send it back to the implementer.
4. **Scan limits matrix (#584).** If the PR changes how scans are counted,
   charged or refused, it must add or change rows in `backend/tests/limits/`.
   That covers `backend/app/ai/quota.py`, `breaker.py`, the limit keys in
   `settings.py`, and the refusals or outcome handling in
   `backend/app/api/scan.py`. A PR that changes those rules without touching
   the matrix is a blocking finding; send it back to the implementer. See
   `docs/claude/testing.md` ("Scan limits matrix").
5. Post the audit result as part of the review-round comment.

## Brief for implementers (paste into the subagent prompt)

> **Add tests.** Add tests that fail without your change. Every AC gets
> coverage and every bug fix gets a regression test. Every new or changed
> endpoint gets tests for auth, rate limit and input validation. Mock AI
> providers at the gateway boundary; never call a real model in tests.
>
> **Your own tests.** You may freely edit tests you added in this branch.
>
> **Existing tests.** For tests that existed before your branch, you may:
>
> - update one, only when an AC intentionally changes the behaviour it asserts;
> - delete one, only when an AC removes the feature;
> - make a mechanical update (imports, renames) that keeps the expected values.
>
> **When an existing test fails.** If no AC explains the failure, assume your
> code is wrong and fix the code. Never skip, xfail, `.only`, deselect, delete,
> loosen, or raise timeouts or thresholds on an existing test to make it pass.
> If you think an existing test's expectation is itself wrong, stop and report
> it instead of changing it.
>
> **Report changes.** Report every existing test you modified, with the AC or
> reason, so it can go in the PR's Test changes table.
