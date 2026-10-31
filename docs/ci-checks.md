# CI checks and branch protection

Every PR into `dev` shows about 40 checks. This page says what each one is for, what it prevents, where it is defined, when it was introduced, how it gates, and roughly how long it takes. It was written for #854 (part of the #803 epic). The model for the layout is BC-Arcade's `docs/CI-CHECKS.md`.

Source of truth is `.github/workflows/`. This page is a snapshot taken from `dev` at `9c0be9f` (full SHA `9c0be9fa4f460a7808cec4eddd7e504332f10f13`, committed 2026-10-30). If it disagrees with the workflow files, the workflow files win. For how the test layers fit together see [testing.md](testing.md); for the native build pipelines see [ios-ci.md](claude/ios-ci.md) and [android-ci.md](claude/android-ci.md); for the git workflow see [git.md](claude/git.md).

The repo merges PRs with **merge commits**. Squash merging is refused: a squash merge through the API returned HTTP 405 "Squash merges are not allowed" on 2026-10-31 while merging the #803 PRs (to confirm in GitHub settings; decision tracked in #858). [git.md](claude/git.md) still says "squash merge into `dev`"; that line is out of date.

## Summary

A PR into `dev` triggers **43 workflow jobs** in 11 workflow files: 27 in `ci.yml`, and 16 in 10 stand-alone workflows (`commitlint`, `design-token-check`, `gemini-policy`, `openai-policy`, `migration-guard`, `static-frontend`, `repo-hygiene` (2 jobs), `doc-reminder`, `e2e-web`, `security` (6 jobs)). Two of the 43 never run on a PR and always show as skipped (`external-links`, `OWASP ZAP Active Scan`). One, `commitlint`, currently never starts (see below). That leaves **40 jobs that report a result**, plus the external `Supabase Preview` row, which also shows as skipped. A PR into `main` skips `security.yml` (6 jobs) and `e2e-web` (1 job) because both only trigger on PRs into `dev`, so it triggers 36 jobs.

- **Required directly by the ruleset file: 30 contexts** (24 from `ci.yml`, 6 from stand-alone workflows). See [Branch protection](#branch-protection).
- **Conditional: 10 jobs.**
  - `js-bundle-check` and `android-bundle-check`: only when `detect-changes` reports a frontend path changed.
  - `ios-build-check` and `android-build-check`: only when it reports a native path changed, and never on draft PRs. `native-build-gate` always runs and stands in for them (and for the two bundle checks).
  - `e2e-web`: only on PRs into `dev` that touch `frontend/**`, `backend/**`, the workflow or the two setup actions.
  - The five `security.yml` PR jobs (`Bandit SAST`, `pip-audit SCA`, `npm audit SCA`, `Trivy filesystem scan`, `pytest -m security`): only on PRs into `dev` that touch `backend/app/**`, `backend/tests/security/**`, `backend/requirements*.txt`, `security.yml`, the two setup actions or `.zap/**`. A frontend-only PR runs none of them.
- **Not required: 13 jobs** (43 − 30). Three of them are orchestration or gate-covered (`detect-changes`, and `ios-build-check`/`android-build-check`, which `native-build-gate` stands in for). The other **8 are advisory**: they run or are meant to run on PRs but are not in the ruleset file, so they cannot block a merge as the file stands: `commitlint`, `doc-reminder`, `e2e-web` and the five `security.yml` jobs. Two more (`external-links`, `OWASP ZAP Active Scan`) never run on a PR.
- **Broken: `commitlint`** (#856). Every run of `commitlint.yml` sampled through the Actions API on 2026-10-31 ended `startup_failure`, including run number 1 (2026-04-11) and the latest, 1092 (run 41168117654). A startup failure creates no check run, so PRs never show a commit-message check. The cause is not visible from here. See [Observations](#observations-suggestions-only).
- **Slowest checks** (run 41164807416): `ios-build-check` 15 min 35 s, `android-build-check` 7 min 5 s (both conditional); then `test-python` 2 min 32 s, `e2e-web` 1 min 54 s (conditional), `test-migrations` 1 min 49 s. Every other job finished in under 70 seconds. These are single samples; the same jobs varied by about ±40% across runs (for example `test-migrations` took 2 min 27 s on #852). On a native PR the chain `test-python` then `ios-build-check` makes the whole run take about 19 minutes.

### How to read the tables

- **Check**: the exact check-run name as GitHub shows it, taken from the check runs of PRs #848 and #852. For a job that calls a reusable workflow it is `<job id> / <inner job name>`. Branch protection matches on that full string.
- **Workflow:job**: the file and job id. Reusable workflows live in `wcmchenry3-stack/.github`, which is not readable from this repository, and every call is pinned to `@main`. Where a purpose can only be inferred from the job name or its `with:` inputs, the cell says "inferred".
- **Introduced**: the earliest commit on `origin/dev` that adds the job id to the workflow file (`git log -S '<job id>:' --reverse`), with the PR or issue number from the commit subject if it has one. Many early jobs first appear in a sync or bulk commit, so this is "first seen on `dev`", not necessarily the first time the job existed. The four org-template workflows (`commitlint`, `design-token-check`, `gemini-policy`, `openai-policy`) were added by "sync community health files from org template" commits; their real origin is "origin unclear".
- **Gating**: **Always** means the job starts on every PR into `dev` and `main`. **Conditional** means a path filter, `if:` or branch filter can stop it starting. **Advisory** means it runs on PRs but is not in `.github/rulesets/protect-main-dev.json`, or it cannot fail. "Tier 0/1/2" is explained in the next section. "Required (file)" means the context is listed in the ruleset file; whether GitHub enforces it is to confirm in GitHub settings.
- **Time**: wall-clock `completed_at - started_at` of the job on a sampled successful run. It excludes queueing and runner start-up. Sampled runs are all on PR #848 (2026-10-31): `ci.yml` run **41164807416**, `repo-hygiene` **41164358342**, `security` **41164358430**, `e2e-web` **41164358382**, `static-frontend` **41164358308**, `migration-guard` **41164358292**, `doc-reminder` **41164358383**, `design-token-check` **41164358579**, `openai-policy-check` **41164358575**, `gemini-policy-check` **41164358627**. `ci.yml` jobs come from the jobs API for that run; the others from the check-run timestamps of the same runs. "skipped on sampled run" means that job did not run on it; "not measured" means no sample.
- A skipped job reports as success to required checks, but a reusable-workflow caller skipped by `if:` reports under its plain job id (`ios-build-check`), never `ios-build-check / Xcode Build (no signing)`. That is why `native-build-gate` exists.

## Job tiers in `ci.yml`

```text
Tier 0 (no needs)
  detect-changes, gate-main-source, secret-scan, lint-python, lint-frontend
        |
        v   needs: lint-python, lint-frontend, secret-scan
Tier 1  backend-health, expo-compat, local-path-check, sentry-check, openai-policy,
        claude-policy, provenance-coverage, feedback-token-lint, test-python,
        test-frontend, cve-python, cve-frontend, sast-python, test-migrations,
        podfile-lock-check, typecheck-frontend, gradle-wrapper-check
        js-bundle-check, android-bundle-check (also need detect-changes)
        |
        v   needs: detect-changes, test-python, test-frontend
Tier 2  ios-build-check, android-build-check
        |
        v   needs: detect-changes, ios-build-check, android-build-check,
            js-bundle-check, android-bundle-check
Gate    native-build-gate (if: always())
```

- `gate-main-source` is Tier 0 but is not in the Tier 1 `needs:` list, so a failure there does not skip Tier 1.
- If `lint-python`, `lint-frontend` or `secret-scan` fails, every Tier 1 and Tier 2 job is skipped (their `needs:` failed). Skipped counts as passing for required checks, but the failed lint job is itself required, so the merge stays blocked.
- `podfile-lock-check` and `gradle-wrapper-check` have `if: github.event_name == 'pull_request'`, so they run on PRs only.
- `backend-health` has `if: github.workflow != 'Deploy'` so a production outage cannot block the deploy that would fix it (the workflow is also called from `deploy.yml`).
- **Stand-alone workflows** (`commitlint`, `design-token-check`, `gemini-policy`, `openai-policy`, `migration-guard`, `static-frontend`, `repo-hygiene`, `doc-reminder`, `e2e-web`, `security`) have no `needs:` and start immediately, in parallel with Tier 0.
- **Draft PRs.** Only `ios-build-check` and `android-build-check` skip on drafts (`github.event.pull_request.draft != true`). Everything else, including `e2e-web` and the `security.yml` jobs, runs on drafts. `ready_for_review` is in the `ci.yml` trigger types, so marking a draft ready starts a new run that includes the native builds.
- **Concurrency.** `ci.yml`: group `ci-${{ github.workflow }}-${{ github.ref }}`, `cancel-in-progress` on every ref except `main`. `repo-hygiene` and `e2e-web` also cancel superseded runs. `static-frontend`, `migration-guard`, `security`, `doc-reminder` and the four org-template workflows set no `concurrency`. A re-push therefore shows an earlier cancelled `ci.yml` run on the old SHA.
- **Timeouts.** `detect-changes`, `claude-policy`, `native-build-gate` and `migration-guard` set `timeout-minutes: 5`; `repo-hygiene` 10; `external-links` 15; `e2e-web` 25. No other job that runs on a PR sets one (GitHub's 360-minute default applies); of the non-PR workflows, `db-backup` (30), `db-restore-drill` (45), `perf-ai` (20) and `ai-eval` (90) do. Reusable-workflow timeouts are not visible from here.
- **`continue-on-error`**: no workflow in the repo sets it.

## Code quality and style

| Check | Purpose | Prevents | Workflow:job | Introduced | Gating | Time |
| --- | --- | --- | --- | --- | --- | --- |
| `lint-python / Lint Python (black + ruff)` | black and ruff on `backend/` (pins in the caller: black 26.10.1, ruff 0.17.1; the comment says keep them in sync with `requirements-dev.txt`) | Unformatted code and lint findings landing in the backend, and an unpinned ruff changing the rules under unrelated PRs | `ci.yml:lint-python` (org `called-lint-python.yml`) | 2026-03-22, f36db91 (#31, sync from main) | Always, Tier 0, required (file) | 16s |
| `lint-frontend / Lint frontend (eslint + prettier)` | eslint and prettier on `frontend/`; the caller passes `run-a11y: true` (an accessibility lint pass, inferred from the input) | Lint errors, formatting drift, accessibility lint findings | `ci.yml:lint-frontend` (org `called-lint-frontend.yml`) | 2026-03-22, f36db91 (#31) | Always, Tier 0, required (file) | 42s |
| `typecheck-frontend` | `npm run typecheck` (`tsc --noEmit`) in `frontend/` | A type error merging, because Jest and Babel do not type-check | `ci.yml:typecheck-frontend` | 2026-10-26, a72bfa6 | Always, Tier 1, required (file) | 41s |
| `expo-compat / Expo Compatibility Check` | `called-expo-compat.yml` on `frontend/`; purpose inferred from the name (Expo SDK compatibility of installed packages; `dependabot.yml` says bumps that break it were seen twice) | A dependency bump outside what the installed Expo SDK supports | `ci.yml:expo-compat` (org `called-expo-compat.yml`) | 2026-03-31, d2e1b0c | Always, Tier 1, required (file) | 33s |
| `design-token-check / Design Tokens & Accessibility Check` | Org design-token and accessibility policy; `docs/testing.md` describes it as the colour-literal gate for CLAUDE.md hard rule 5. Body not readable from here | Hard-coded colours and accessibility regressions in the UI | `design-token-check.yml:design-token-check` (org `called-design-token-check.yml`) | 2026-04-11, f620aff (org template sync); origin unclear | Always (also re-runs when a PR is edited), required (file) | 22s |
| `commitlint / <inner name unknown>` | Conventional Commits check of PR title or commits (inferred from the name; CLAUDE.md names CI commitlint as the real enforcement of the commit format) | Non-conventional commit messages breaking release-please changelogs | `commitlint.yml:commitlint` (org `called-commitlint.yml`) | 2026-04-11, 31bc083 (org template sync); origin unclear | **Advisory and broken**: not in the ruleset file, and the workflow ends in `startup_failure` so no check run is ever created | n/a (never starts) |

## Tests

| Check | Purpose | Prevents | Workflow:job | Introduced | Gating | Time |
| --- | --- | --- | --- | --- | --- | --- |
| `test-python / Test Python (pytest)` | Backend pytest with coverage, `coverage-threshold: 98` passed by the caller. Runs the `security`-marked tests too (no marker filter in `pytest.ini`) | A backend regression or coverage drop | `ci.yml:test-python` (org `called-test-python.yml`) | 2026-03-22, f36db91 (#31) | Always, Tier 1, required (file) | 2m 32s |
| `test-frontend / Test frontend (jest/vitest)` | Jest with coverage; the caller passes `coverage-threshold: 80`, while the Jest floors in `frontend/package.json` are higher and fail locally too | A frontend regression or coverage drop | `ci.yml:test-frontend` (org `called-test-frontend.yml`) | 2026-03-22, f36db91 (#31) | Always, Tier 1, required (file) | 52s |
| `test-migrations` | On a Postgres 16 service: `alembic upgrade head`, `alembic check`, `alembic downgrade -1`, then `alembic upgrade head` and `pytest -m integration tests -q -rs` with `REQUIRE_DB_TESTS=1` (every integration-marked test in every folder) | A migration that fails on Postgres, model drift from the migrations, a broken downgrade, and integration tests silently skipping | `ci.yml:test-migrations` | 2026-03-27, ac9ee90 (#49) | Always, Tier 1, required (file) | 1m 49s |
| `e2e-web` | Playwright (Chromium) against the static web export, a real backend on a scratch Postgres 16, fake AI provider and catalog, axe WCAG checks. Uploads a report artifact on failure. Details in [testing.md](testing.md#web-e2e-playwright-470) | A user-visible web flow breaking while unit tests stay green | `e2e-web.yml:e2e-web` | 2026-10-30, 115a709 (#470) | **Conditional and advisory**: PRs into `dev` touching `frontend/**`, `backend/**`, `e2e-web.yml` or the two setup actions. Not in the ruleset file | 1m 54s |

## Security and supply chain

| Check | Purpose | Prevents | Workflow:job | Introduced | Gating | Time |
| --- | --- | --- | --- | --- | --- | --- |
| `secret-scan / Secret scan (gitleaks)` | gitleaks (inferred from the inner name) | A committed API key or token | `ci.yml:secret-scan` (org `called-secret-scan.yml`) | 2026-03-21, 33bcf79 (#23) | Always, Tier 0, required (file) | 7s |
| `cve-python / CVE scan Python (pip-audit)` | pip-audit on `backend/` | A known-vulnerable Python dependency | `ci.yml:cve-python` (org `called-cve-python.yml`) | 2026-03-22, f36db91 (#31) | Always, Tier 1, required (file) | 19s |
| `cve-frontend / CVE scan frontend (npm audit)` | npm audit at `audit-level: critical` on `frontend/` | A critical npm advisory shipping | `ci.yml:cve-frontend` (org `called-cve-frontend.yml`) | 2026-03-22, f36db91 (#31) | Always, Tier 1, required (file) | 33s |
| `sast-python` | Bandit 1.8.3 over `backend/app/` (`-c .bandit -ll`), then fails on any HIGH or MEDIUM finding. Uploads `bandit-report` | Insecure Python patterns merging | `ci.yml:sast-python` | 2026-03-28, 8eecbf5 | Always, Tier 1, required (file) | 10s |
| `Bandit SAST` | The same Bandit command and failure rule as `sast-python`, plus a weekly run | Same as `sast-python` (duplicate) | `security.yml:sast-bandit` | 2026-03-28, 8eecbf5 | **Conditional, advisory**: PRs into `dev` on the `security.yml` paths; also weekly (Mondays 08:00 UTC) and manual | 14s |
| `pip-audit SCA` | `pip-audit -r requirements.txt`, report uploaded. No ignore list | Same as `cve-python` (overlap) | `security.yml:sca-pip-audit` | 2026-03-28, 8eecbf5 | **Conditional, advisory**: as above | 25s |
| `npm audit SCA` | `npm audit --audit-level=critical --json > npm-audit-report.json \|\| true`, then uploads the report. **There is no step that reads the report, so the job cannot fail on a finding.** Node 20 | Nothing as written; it only produces an artifact | `security.yml:sca-npm-audit` | 2026-03-28, 8eecbf5 | **Conditional, advisory, cannot fail on findings**: as above | 58s |
| `Trivy filesystem scan` | Trivy `fs` scan of the whole repo, `exit-code: 1`, `ignore-unfixed`, severity CRITICAL and HIGH, suppressions in `.trivyignore` | A known-vulnerable dependency anywhere in the tree, including lockfiles | `security.yml:sca-trivy` | 2026-03-29, 9e318e2 | **Conditional, advisory**: as above. The path list has no `frontend/**` entry, so a frontend-only PR never runs it | 17s |
| `pytest -m security` | `pytest -m security -v` with `TURNSTILE_REQUIRED=false` | Regressions in request hardening and access control show up as their own check. These tests also run inside `test-python` | `security.yml:pytest-security` | 2026-03-28, 8eecbf5 | **Conditional, advisory**: as above | 1m 2s |
| `OWASP ZAP Active Scan` | ZAP full scan of `https://bookshelfapi.buffingchi.com` with `.zap/rules.tsv`, `fail_action: true` | Injection and similar issues reachable on the live API | `security.yml:dast-zap` | 2026-03-28, 8eecbf5 | Never on PRs: `if:` schedule or manual dispatch only, so it shows as skipped | skipped on sampled run |

## Policy and compliance

| Check | Purpose | Prevents | Workflow:job | Introduced | Gating | Time |
| --- | --- | --- | --- | --- | --- | --- |
| `gate-main-source / Verify PR source is dev` | Org-defined; from its name, a PR into `main` must come from `dev` (inferred; it also runs on PRs into `dev`) | A feature branch merged straight into `main` | `ci.yml:gate-main-source` (org `called-gate-main-source.yml`) | 2026-03-22, f36db91 (#31) | Always, Tier 0, required (file) | 3s |
| `claude-policy` | `python3 scripts/policy/check_claude_policy.py`: scans source that touches the Anthropic SDK or lives under `app/ai/` against `.claude/policies/claude.md` (stdlib only, #537) | AI calls that bypass the gateway rules | `ci.yml:claude-policy` | 2026-10-22, e832c31 (#537) | Always, Tier 1, required (file) | 5s |
| `openai-policy / OpenAI API Policy Check` | Org OpenAI policy workflow, called from `ci.yml` | A hard-coded OpenAI key or a use that violates the policy (inferred from the name) | `ci.yml:openai-policy` (org `called-openai-policy.yml`) | 2026-03-28, 6c08344 | Always, Tier 1, required (file) | 5s |
| `openai-policy-check / OpenAI API Policy Check` | The same org workflow, called again from its own stand-alone workflow | Same as above (duplicate call) | `openai-policy.yml:openai-policy-check` (org `called-openai-policy.yml`) | 2026-04-11, 3297d41 (org template sync); origin unclear | Always (also re-runs on PR edit), required (file) | 6s |
| `gemini-policy-check / Gemini API Policy Check` | Org Gemini policy workflow. The body is not readable from here | A Gemini integration landing without the policy (inferred) | `gemini-policy.yml:gemini-policy-check` (org `called-gemini-policy.yml`) | 2026-04-11, 1ba1392 (org template sync); origin unclear | Always (also re-runs on PR edit), required (file) | 5s |
| `provenance-coverage / Provenance Coverage` | Org-defined; the repo does not show what it inspects | Origin unclear; confirm in the org workflow | `ci.yml:provenance-coverage` (org `called-provenance-coverage.yml`) | 2026-04-15, 9d5b1fc (#273) | Always, Tier 1, required (file) | 7s |
| `feedback-token-lint / Feedback Token Lint` | Org-defined; the repo does not show what it inspects | Origin unclear; confirm in the org workflow | `ci.yml:feedback-token-lint` (org `called-feedback-token-lint.yml`) | 2026-04-14, 64a6083 | Always, Tier 1, required (file) | 7s |
| `sentry-check / Sentry Check` | Org-defined Sentry check for `backend/`, run with the `SENTRY_BC_BOOKSHELF` secret as `SENTRY_DSN` | Backend Sentry misconfiguration (inferred; exact assertions not visible) | `ci.yml:sentry-check` (org `called-sentry-check.yml`) | 2026-03-31, d2e1b0c | Always, Tier 1, required (file) | 29s |
| `local-path-check / Local Path Detection` | Scans `frontend/ios/BookShelfAI.xcodeproj` and `frontend/android` for `*.pbxproj`, `*.xcconfig`, `*.gradle`, `*.properties` (inputs; the match rule is inferred to be absolute local paths) | A developer's local path committed into the Xcode or Gradle project | `ci.yml:local-path-check` (org `called-local-path-check.yml`) | 2026-03-31, d2e1b0c | Always, Tier 1, required (file) | 4s |
| `backend-health / Backend Health` | Probes `/health` on the Render origin `https://bookshelf-api-rxp3.onrender.com` (the workflow comment explains it bypasses Cloudflare on purpose) | A deployed backend that does not answer. It depends on an external service being up | `ci.yml:backend-health` (org `called-backend-health.yml`) | 2026-03-31, d2e1b0c | Always on PRs, Tier 1, required (file); skipped when called from `deploy.yml` | 3s |

## Mobile builds

iOS and Android release builds are made by Xcode Cloud and Gradle, not by these jobs. These are pre-merge guards that the native projects and JS bundles still build.

| Check | Purpose | Prevents | Workflow:job | Introduced | Gating | Time |
| --- | --- | --- | --- | --- | --- | --- |
| `ios-build-check / Xcode Build (no signing)` | `called-ios-build-check.yml` for workspace `BookShelfAI.xcworkspace`, scheme `BookShelfAI` (inputs; the unsigned build is inferred from the inner name) | A change that breaks the iOS build | `ci.yml:ios-build-check` (org `called-ios-build-check.yml`) | 2026-03-31, d2e1b0c | **Conditional**: `detect-changes` `native == 'true'` and not a draft. Tier 2 (needs `test-python`, `test-frontend`). Not in the ruleset file; covered by `native-build-gate`. Skipped, it shows as plain `ios-build-check` | 15m 35s (run 41164807416; skipped on run 41164358656) |
| `android-build-check / Gradle Build (Debug)` | `called-android-build-check.yml` on `frontend/` (debug Gradle build, from the inner name) | A change that breaks the Android build | `ci.yml:android-build-check` (org `called-android-build-check.yml`) | 2026-04-02, 8c16dfb | **Conditional**: same rule as iOS. Covered by `native-build-gate` | 7m 5s (run 41164807416; skipped on run 41164358656) |
| `js-bundle-check` | `expo export:embed` for iOS, then fails if `main.jsbundle` is missing or under 1000 bytes | A silent bundling failure that Xcode Cloud would only find later | `ci.yml:js-bundle-check` | 2026-04-01, 13dcbcc | **Conditional**: `detect-changes` `frontend == 'true'`. Required (file); skipped counts as passing | 1m 6s (skipped on run 41168117780, backend-only PR #852) |
| `android-bundle-check` | `expo export:embed` for Android, same size check (`index.android.bundle`). The job name comes from the guardrails commit; its comment still says "before EAS Build", which the project does not use | A silent Android bundling failure | `ci.yml:android-bundle-check` | 2026-04-02, 8c16dfb | **Conditional**: as `js-bundle-check`. Required (file) | 1m 1s |
| `podfile-lock-check` | Fails if `frontend/ios/Podfile` changed in the PR diff but `Podfile.lock` did not | Pod version drift and runtime crashes on Xcode Cloud (CLAUDE.md rule 11) | `ci.yml:podfile-lock-check` | 2026-04-01, b7a0f5d | Always on PRs (push events skip it), Tier 1, required (file). It runs unconditionally and only fails when `Podfile` changes | 7s |
| `gradle-wrapper-check` | `gradle/actions/wrapper-validation@v6` checks the wrapper jar checksum; a second step only prints a notice when Gradle build files change and cannot fail | A tampered Gradle wrapper (supply chain) | `ci.yml:gradle-wrapper-check` | 2026-04-02, 8c16dfb | Always on PRs, Tier 1, required (file) | 8s |

## Docs and repo hygiene

| Check | Purpose | Prevents | Workflow:job | Introduced | Gating | Time |
| --- | --- | --- | --- | --- | --- | --- |
| `repo-hygiene` | Steps run in order and stop at the first failure (only the jscpd report upload, `if: always()`, still runs): `scripts/check_file_length.py` (600-line cap), vulture dead-code check on `backend/app`, jscpd copy-paste threshold (`.jscpd.json`), then lychee on `docs/**/*.md`, `README.md`, `CLAUDE.md` with `--offline --include-fragments` (relative links and anchors only). See [testing.md](testing.md#repo-hygiene-checks) | Growing monoliths, dead backend code, copy-paste growth, dead documentation links and renamed headings | `repo-hygiene.yml:repo-hygiene` | 2026-10-30, 32221f3 (#769; vulture added later in c91a0ee) | Always (no branch filter), required (file) | 12s |
| `external-links` | lychee including external URLs | Dead external links in the docs | `repo-hygiene.yml:external-links` | 2026-10-30, 32221f3 (#769) | Never on PRs: only the Monday 06:17 UTC schedule and manual dispatch; shows as skipped | skipped on sampled run |
| `static-frontend` | `npm run knip` in `frontend/` (unused files, exports, dependencies) | Dead frontend code and unused dependencies accumulating | `static-frontend.yml:static-frontend` | 2026-10-30, 61ebc88 (#768) | Always (deliberately no `paths:` filter so it can be required), required (file) | 31s |
| `migration-guard` | `scripts/migration_guard/check_destructive_migrations.py` over migrations the PR adds or changes; fails on a destructive `upgrade()` step unless the PR has the label `destructive-migration-approved`. Re-runs on label changes. On PRs into `main` it exits 0 without scanning | A destructive Alembic migration (drop, rename, type change) merging without owner approval | `migration-guard.yml:migration-guard` | 2026-10-27, 330ebca (#687) | Always, required (file); a no-op on PRs into `main` | 7s |
| `doc-reminder` | `actions/github-script` maps changed paths to docs (mapping is inline in the workflow) and keeps one comment per PR, found by `<!-- doc-reminder:v1 -->`. Its own header says it never fails CI | Behaviour changes merged without the doc that describes them | `doc-reminder.yml:doc-reminder` | 2026-04-04, 082ae02 | **Advisory**: runs on opened, synchronize, reopened (not `ready_for_review`), any base branch; not in the ruleset file | 3s |

## Gating and orchestration

| Check | Purpose | Prevents | Workflow:job | Introduced | Gating | Time |
| --- | --- | --- | --- | --- | --- | --- |
| `detect-changes` | `git diff --name-only` against the PR base sets two outputs. `native` is true for `frontend/ios/`, `frontend/android/`, `frontend/plugins/`, `frontend/app.json`, `frontend/app.config.*`, `frontend/package.json`, `frontend/package-lock.json` or `.github/workflows/ci.yml`. `frontend` is true for anything under `frontend/`, `ci.yml` or `.github/actions/setup-frontend/`. On push and `workflow_call` both are true; if the diff cannot be computed both are true (fails closed) | Running the expensive bundle and native builds on every PR | `ci.yml:detect-changes` | 2026-10-30, 7b88cf8 (#767) | Always, Tier 0, 5 min timeout. Not in the ruleset file, but `native-build-gate` fails if it did not succeed | 6s |
| `native-build-gate` | `if: always()`. Passes when `ios-build-check`, `android-build-check`, `js-bundle-check` and `android-bundle-check` each ended `success` or `skipped`, and `detect-changes` ended `success`. Any other result, **including `cancelled`**, fails it | A skipped reusable-workflow job having no stable name to require | `ci.yml:native-build-gate` | 2026-10-30, 7b88cf8 (#767) | Always, required (file), 5 min timeout | 2s |
| Concurrency and cancelled runs | See [Job tiers](#job-tiers-in-ciyml) | n/a | `ci.yml` `concurrency` | not traced | n/a | n/a |

## External apps (not in workflow files)

None of these is defined under `.github/workflows/`, so their configuration is "to confirm in GitHub settings".

| Check | What it is | Evidence | Notes |
| --- | --- | --- | --- |
| `Supabase Preview` | Supabase GitHub integration. Its details link points at the project's Supabase branches page | Appears as **skipped** on #848 and #852 | The reason it is skipped is not visible from the repo. Never make it required |
| Codex review | The Codex GitHub app, used by the `ship-story` skill (`@codex review` comment; the skill treats "no response" as "Codex not available") | No check run on #848 or #852 | A comment or review bot, not a status check. Whether it is installed on this repo: to confirm in GitHub settings |
| CodeQL | GitHub code scanning | No CodeQL workflow in the repo and no CodeQL check on #848 or #852 | Not present as far as can be seen. Default setup would show check runs; none seen. To confirm in GitHub settings (Security, Code scanning) |
| Dependabot | `.github/dependabot.yml`: weekly (Monday 09:00 America/Los_Angeles) pip, npm and github-actions updates, with Expo and React Native minor and major bumps ignored | The PRs it opens run the same checks as any PR | Not a check |
| Secret scanning | GitHub's own secret scanning; `.github/secret_scanning.yml` excludes `backend/tests/**` and `**/.env.example` | Config file only | Separate from the `secret-scan` gitleaks job. Whether push protection is on: to confirm in GitHub settings |

## Workflows that do not run on PRs

| Workflow | Trigger | What it does |
| --- | --- | --- |
| `deploy.yml` | push to `main` | Calls `ci.yml` as a reusable workflow, then deploys `bookshelf-api` and `bookshelf-web` to Render through the org `called-deploy-render.yml`. Deploy jobs are skipped while their `RENDER_PROD_*_SERVICE_ID` variable is blank |
| `release-please.yml` | push to `main` | Opens and updates release PRs (inferred from the workflow name; the body is the org `called-release-please.yml`) |
| `sync-main-to-dev.yml` | push to `main` | Opens a PR syncing `main` back into `dev` (org workflow pinned to a commit SHA) |
| `sync-native-versions.yml` | release published | Bumps the iOS and Android build numbers after a release-please release |
| `db-backup.yml` | daily 07:17 UTC, manual | Encrypted `pg_dump` of the production database to R2 |
| `db-restore-drill.yml` | manual | Restores the latest backup into a throwaway Postgres and runs sanity checks |
| `perf-ai.yml` | nightly 03:17 UTC, manual | Locust load run of `POST /scan` against a fake provider |
| `ai-eval.yml` | manual | Labelled scan evaluation against a real, paid model |
| `zap-scan.yml` | Sundays 06:00 UTC, manual | Org `called-zap-scheduled.yml` against the `SITE_URL` secret |
| `security.yml` | Mondays 08:00 UTC, manual (and the PR runs above) | The scheduled run also executes `OWASP ZAP Active Scan` |
| `repo-hygiene.yml` | Mondays 06:17 UTC, manual (and PRs) | The scheduled run is only `external-links` |
| `expo-sdk-upgrade-tracker.yml` | Mondays 09:00 UTC, manual | Keeps one tracking issue for the next Expo SDK |
| `set-sentry-dsn.yml` | manual | Takes Sentry DSN inputs for the backend and frontend; what it writes is to confirm in the workflow |
| `ci.yml`, `repo-hygiene.yml`, `static-frontend.yml` | push to `dev` and `main` (`static-frontend` only `dev`) | The same jobs again after a merge; `detect-changes` then reports everything true |

## Branch protection

`.github/rulesets/protect-main-dev.json` is a ruleset export. GitHub does not read it from the repo; the owner imports it in Settings, Rules, Rulesets. Whether it has been imported, is `active`, and has not drifted is **to confirm in GitHub settings**.

What the file declares for `refs/heads/main` and `refs/heads/dev`, with enforcement `active` and no bypass actors:

- block deletion, block non-fast-forward (force) pushes, block creation;
- `required_linear_history`;
- pull requests: 1 approving review, stale reviews dismissed on push, code-owner review required, last-push approval not required, allowed merge methods `squash` and `merge`;
- required status checks, with `strict_required_status_checks_policy: true` (branch must be up to date): the 30 contexts listed under [Recommended required checks](#recommended-required-checks) below.

`.github/CODEOWNERS` names `@wcmchenry3` as owner of everything, including `.github/`, `backend/app/auth/`, `backend/app/core/`, `backend/tests/security/` and the migration guard.

**Things that only GitHub settings can confirm:**

- whether the ruleset above (or any classic branch protection) is applied, and to which branches;
- merge methods actually enabled. Squash was refused with HTTP 405 on 2026-10-31 (see #858), which contradicts the file's `allowed_merge_methods` list; merge commits are what is used. `required_linear_history` as written would normally forbid merge commits, so the live settings probably differ from the file. See [Observations](#observations-suggestions-only);
- whether the single code owner can satisfy "1 approving review" and "code-owner review" on PRs they opened themselves;
- whether any check outside the file (for example `Supabase Preview`) is required.

`backend/tests/unit/test_ruleset_required_checks.py` (run by `test-python`) fails when a required context matches no workflow job, when its workflow has a `paths` filter or does not run on PRs into `dev`, or when a native build job is required directly. It does not check inner names after the `/`.

### Recommended required checks

These are recommendations, not a record of current settings. Rules of thumb: a required name must exist on every PR, so required jobs must always start; a job skipped by `if:` passes; a path-filtered workflow that does not trigger never reports and would block the PR. Copy names from a PR's checks list, not from this page, when applying.

**Keep as in the ruleset file** (all of these always start on a PR into `dev` or `main`):

- `secret-scan / Secret scan (gitleaks)`, `lint-python / Lint Python (black + ruff)`, `lint-frontend / Lint frontend (eslint + prettier)`, `typecheck-frontend`, `static-frontend`, `repo-hygiene`
- `test-python / Test Python (pytest)`, `test-frontend / Test frontend (jest/vitest)`, `test-migrations`, `migration-guard`
- `cve-python / CVE scan Python (pip-audit)`, `cve-frontend / CVE scan frontend (npm audit)`, `sast-python`
- `native-build-gate` (stands in for `ios-build-check` and `android-build-check`, which are never required directly because skipped they report under a different name), `js-bundle-check`, `android-bundle-check`, `podfile-lock-check`, `gradle-wrapper-check`, `expo-compat / Expo Compatibility Check`, `local-path-check / Local Path Detection`
- `claude-policy`, `openai-policy / OpenAI API Policy Check`, `openai-policy-check / OpenAI API Policy Check`, `gemini-policy-check / Gemini API Policy Check`, `design-token-check / Design Tokens & Accessibility Check`, `gate-main-source / Verify PR source is dev`, `provenance-coverage / Provenance Coverage`, `feedback-token-lint / Feedback Token Lint`, `sentry-check / Sentry Check`

**Candidates to leave advisory or review:** `backend-health / Backend Health` (it probes a live Render service, so an outage unrelated to the PR blocks every merge; it is in the ruleset file today).

**Not to require:** `e2e-web` and every `security.yml` job (path-filtered and `dev`-only; a PR outside the paths would block), `doc-reminder`, `detect-changes`, `external-links`, `OWASP ZAP Active Scan`, `Supabase Preview`. `commitlint` only after it starts reporting.

**PRs into `main`:** the same list applies. `security.yml` and `e2e-web` do not trigger, `migration-guard` passes without scanning, and the native and bundle checks follow `detect-changes` exactly as on `dev` (a release PR from `dev` can change native paths, in which case both native builds run). `gate-main-source` is the check that matters most there.

## Observations (suggestions only)

These are not decisions.

- **`commitlint` never starts** (#856). Every run of `commitlint.yml` sampled through the Actions API on 2026-10-31 (numbers 1, 592 to 596, 1088 to 1092) is `startup_failure`; no check run is created, so it is invisible on the PR and does not block. A startup failure usually means the caller cannot grant what the called workflow asks for or the called workflow is invalid (the `release-please.yml` comment describes the same failure for missing `permissions:`), but the cause here is not visible from this repo. The reusable workflow is read from `@main` of the org repo, so a change there can break it without any change here. The PR-title `pr-title-gate` hook is local to Claude sessions only. Suggestion: open one failed run's annotations in the Actions tab.
- **`security.yml`'s path filter hides Trivy from frontend-only PRs.** The PR paths are `backend/app/**`, `backend/tests/security/**`, `backend/requirements*.txt`, `security.yml`, the two setup actions and `.zap/**`. A change to `frontend/package.json` or `frontend/package-lock.json` matches none of them, so `Trivy filesystem scan` (and `npm audit SCA`) does not run. #846's node-forge fix was only verified through a backend PR that happened to trigger the workflow. `cve-frontend` (critical only) is the sole dependency scan on a frontend-only PR. Suggestion: add `frontend/package*.json` (and `e2e` lockfiles, if any) to the paths, or run Trivy from `ci.yml`. A path-filtered workflow must not be made required (see above).
- **`native-build-gate` can fail when a superseded run is cancelled** (observed during #803; no run ID recorded). The `ci.yml` concurrency group cancels in-progress runs on every ref except `main`. The gate is `if: always()` and exits 1 for any `needs` result other than `success` or `skipped`, so if the gate itself still runs, a cancelled `ios-build-check` (or any other needed job) turns the old SHA's gate red. Only the newest SHA matters for merging, so it is noise, not a blocked merge. Suggestion: have the gate skip the evaluation when its own run is being cancelled, or treat `cancelled` as neutral.
- **Timing-sensitive tests.** The known case was three scan-cancellation tests (`tests/unit/test_ai_ledger.py`, `tests/unit/test_ai_breaker.py`, `tests/integration/test_ai_usage_db.py`) that cancelled the whole scan pipeline on a fixed 0.2 to 0.5 s deadline. On a loaded runner the pre-provider steps could outlast it, so `test-python` went red with no code at fault (seen on #845). #850 fixed them to cancel from inside the provider call. Look for the same pattern (a wall-clock `move_on_after` or `sleep` around more than the step under test) before calling a red `test-python` a flake. For scale: 18 backend test files use `time.sleep` or `asyncio.sleep`, and about 40 frontend test files use `setTimeout`, fake timers or `waitFor`.
- **Duplicated work.** `openai-policy` (in `ci.yml`) and `openai-policy-check` (stand-alone) call the same org workflow and both are required. `sast-python` and `Bandit SAST` run the same Bandit command. `cve-python` and `pip-audit SCA`, and `cve-frontend` and `npm audit SCA`, scan the same manifests. `pytest -m security` re-runs tests already inside `test-python`. Two ZAP workflows exist (`zap-scan.yml` and `security.yml:dast-zap`) with targets `SITE_URL` and `https://bookshelfapi.buffingchi.com`; whether they overlap is not clear from here.
- **A check that can never fail on findings:** `npm audit SCA` ends with `|| true` and no later step reads the report. `gradle-wrapper-check`'s second step and `doc-reminder` are advisory by design. `external-links`, `OWASP ZAP Active Scan` and `Supabase Preview` always show as skipped on PRs, which adds three rows of noise to the checks list.
- **Native builds are triggered broadly.** `native` is true whenever `frontend/package.json`, `frontend/package-lock.json` or `ci.yml` changes, so every dependency bump and every CI edit pays for a 15-minute iOS build.
- **Drafts run almost everything.** Only the two native builds skip on drafts; `e2e-web` and the `security.yml` jobs also run on every push to a draft.
- **`repo-hygiene` reports one failure at a time.** Its steps have no `if: !cancelled()`, so a file-length failure hides any later jscpd or lychee failure until the next push. Unlike the other stand-alone workflows it has no `branches:` filter, so it also runs on PRs into any other branch.
- **Merge method mismatch.** The ruleset file requires linear history and lists `squash` and `merge`; the repository has squash disabled and merges with merge commits, and [git.md](claude/git.md) says squash into `dev`. One of the three should be updated once the live settings are confirmed.
- **Reusable workflows are in another repo and pinned to `@main`.** `provenance-coverage`, `feedback-token-lint`, `sentry-check`, `gate-main-source`, `commitlint` and the policy checks cannot be explained from here, and a change on the org's `main` changes these checks across all repos at once. A short header comment in each `called-*.yml`, or pinning to a SHA as `sync-main-to-dev.yml` does, would close both gaps.
- **Stale statements elsewhere:** [docs/claude/testing.md](claude/testing.md) says `test-python` runs with `--cov-fail-under=90`; `ci.yml` passes 98. `android-bundle-check` has a comment that still mentions EAS Build.
- **#820 (ratchet the gates)** runs after this story and must update this page in the same PR.
