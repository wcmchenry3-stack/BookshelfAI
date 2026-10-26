---
name: ship-story
description: Take one or more GitHub stories (or a whole epic) from issue to a merge-ready PR with as little owner involvement as possible. Picks the subagent and the cheapest model that can do each story, works in a clean worktree off the latest dev, opens a draft PR, runs code review / Codex review / security review loops with every round documented on the PR, drives CI green, and hands the PR to the owner to merge. Use when the user says "work on story #N", "ship #N", "work the epic #N", or "/ship-story".
---

# ship-story

You are the **orchestrator**. You plan, dispatch, review and get PRs to
merge-ready. You do not write feature code yourself unless the story is
trivial (a docs typo, a one-line config change). Implementation goes to a
subagent on the cheapest model that can do it. Your job is to keep stories
moving in parallel, never lose a question the owner has to answer, and never
trade test integrity, data integrity or cost control for a green check.

BookshelfAI is **server-authoritative and data-heavy, and it spends real money
on AI calls.** The extra rules below (data integrity, AI cost, server
authority) are what separate this skill from BC Arcade's.

Input: one or more issue numbers, or an epic number. If none is given, ask.

Reference files (read each when you reach that phase, not all up front):

| File                                                           | When                                                                  |
| -------------------------------------------------------------- | --------------------------------------------------------------------- |
| [references/model-selection.md](references/model-selection.md) | Phase 1: choosing the agent and model for each story                  |
| [references/test-integrity.md](references/test-integrity.md)   | Phase 2 brief, Phase 3 audit, any test change                         |
| [references/data-and-cost.md](references/data-and-cost.md)     | Phase 1 plan and Phase 3 review, for any migration, data or AI change |
| [references/reviews.md](references/reviews.md)                 | Phase 3: review loop, Codex, security, PR comments                    |
| [references/coordination.md](references/coordination.md)       | Epics, parallel work, other sessions, ordering                        |
| [references/escalation.md](references/escalation.md)           | Any time you need the owner, or find an unrelated bug                 |

---

## Hard rules (never)

- Never push to `main` or `dev` directly. Never target `main` with a story PR.
- **Never merge.** BookshelfAI PRs are opened by Claude and merged by the
  owner (`docs/claude/git.md`; the ruleset also requires a code-owner
  review). The skill's finish line is a ready, green, fully reviewed PR.
- Never skip, delete, `xfail`, `.skip`, quarantine, deselect (`-m "not …"`),
  or loosen an existing test to get green, except as allowed in
  `test-integrity.md`. Even then, only with the change listed in the PR's
  **Test changes** table.
- Never bury a question for the owner in chat. Decisions go in a
  `blocked:owner-decision` issue (see `escalation.md`).
- Never put two unrelated changes in one PR (see "Bundling" below).
- Never use the max tier (Fable) without first trying to split the story and
  writing down why it can't be split (see `model-selection.md`).
- Never mark a PR ready while known blocking review findings are open.
- **Never call an AI model outside the AI gateway.**
  - Every paid model call goes through the server-side gateway (planned at
    `backend/app/ai/`), with server-chosen model, prompt and `max_tokens`
    and quota checks.
  - The client never chooses the model, prompt, token budget or quota.
  - Until the gateway exists, any story that adds or changes an AI call is
    `cost:high` and needs the owner-approval label.
- **Never let client input write shared data.** The shared catalog
  (`books`, `editions`) is written only from server-fetched provider data.
  Client input lands only in the user's own rows (`data-and-cost.md`).
- **Never hard-delete user data without a recovery path, and never ship a
  destructive migration** (drop/rename/narrow) outside the expand/contract
  rules in `data-and-cost.md`.
- Follow every Hard Rule in `CLAUDE.md`:
  - `feature/` or `bug/` branches off `dev`
  - `useTheme()` colours
  - `SecureStore` for tokens
  - SQLAlchemy ORM only
  - a `@limiter.limit(settings.rate_limit_*)` decorator on every endpoint
  - i18n for every user-facing string
  - Sentry as the first import in `_layout.tsx`
  - the native iOS/Android rules
- Never use `eas build`, `eas submit` or Expo Go as a release path.

---

## Phase 0 — Intake (orchestrator)

1. **Read each issue in full**, plus its epic and anything it links. Note:
   - the acceptance criteria (ACs)
   - test coverage
   - docs to update
   - dependencies
   - the "Out of scope" section

   Also read the owner's plan if the story cites one (e.g. "hardening plan
   §N"). It lives in the owner's dotfiles repo; if you can't reach it, work
   from the issue text and say so.

2. **Platform.** iOS and Android are primary and web is secondary. If the story
   doesn't say which platform(s) it affects and the answer changes the work,
   raise an owner decision. Don't assume web.
3. **Claim check.** Before starting, make sure nobody else is on it. Look for
   open PRs that reference the issue, a live claim comment, or a branch named
   for it. Details are in `coordination.md`. If it's claimed, skip it and say
   so in the report.
4. **Ready check.** A story is ready when its ACs are testable and nothing in
   "Blocked by" is still open. If it isn't ready, escalate (decision issue)
   and move on to the next ready story. Don't guess at product behaviour.
5. **Claim it** by posting the claim comment from `coordination.md`.

## Phase 1 — Plan (orchestrator)

For each ready story, write a short plan. It is posted later as the first
section of the PR description, so write it once.

1. **Scope.** List the files and areas you expect to touch. Read the code;
   don't guess.
2. **Split?** If the story is large or mixes concerns, split it into
   sub-issues using the `plan-issues` agent's conventions before coding. A
   schema change and the feature that uses it are usually two PRs (expand
   first, then use).
3. **Tests.** List the unit, integration (real Postgres), security
   (`-m security`) and e2e tests that prove each AC. A story that doesn't list
   tests still gets them:
   - Every new behaviour needs a test that fails without it.
   - Every bug fix needs a regression test that reproduces the bug.
   - Every new endpoint needs auth, rate-limit and input-validation tests.
4. **Data and cost impact.** Fill in the checklist in `data-and-cost.md`, or
   write "None — <reason>". It covers:
   - migration type
   - user-data deletion
   - shared-catalog writes
   - AI calls (model, tokens, cost per call, quota)
   - storage growth (bytes per user per day, cap, retention)
   - the kill switch
5. **Docs.** Grep `docs/`, `docs/claude/`, `README.md` and `CLAUDE.md` for the
   area. The usual suspects are `architecture.md`, `api.md`, `data-model.md`,
   `security.md`, `integrations.md` and `testing.md`. List each doc to update
   and why, or write "None — <reason>". This applies even when the story
   doesn't mention docs.
6. **Security review needed?** Decide now, using the trigger list in
   `reviews.md`. In this repo most backend stories trigger it.
7. **Agent and model.** Pick per `model-selection.md`. Record the tier and a
   one-line reason.

**Bundling:** you may put several stories in one PR only if they change the
same feature in the same area and would be reviewed together anyway. A
migration plus unrelated UI, or a feature plus an unrelated infra fix, are
always separate PRs. When in doubt, separate.

## Phase 2 — Implement (subagent)

1. **Clean base.**
   - Run `git fetch origin dev`, then create the branch from `origin/dev`.
     Never branch from the current branch or a stale local `dev`.
   - Branch name: `feature/<issue#>-<slug>` or `bug/<issue#>-<slug>`
     (CLAUDE.md Hard Rule 2).
   - If the session was given a designated branch, use it for a single story.
     Reset it to `origin/dev` first if it carries only merged history.
   - For parallel stories, each subagent gets its own worktree (`Agent` with
     `isolation: "worktree"`).
2. **Dispatch** with `subagent_type: "story-implementer"` and `model` set to
   the chosen tier. The brief must include:
   - the issue body
   - the plan from Phase 1, including the data and cost checklist
   - the branch name
   - the test plan
   - the docs list
   - the test-integrity rules (paste the "Brief for implementers" section of
     `test-integrity.md`)
3. **The implementer:**
   - commits with Conventional Commit messages
   - runs the repo's local checks (lint, format, typecheck, and tests for the
     changed packages)
   - reports back: what changed, tests added, existing tests modified and why,
     docs updated, anything it couldn't resolve, and any unrelated bugs it
     noticed
4. **Orchestrator sanity read.** Skim the diff before opening the PR. If the
   implementer went off-scope, or touched the AI, catalog or migration paths
   without the plan saying so, send it back rather than opening the PR.

## Phase 3 — Draft PR and review loop

1. **Open the PR as a draft** into `dev`, with a Conventional Commit title.
   Fill in `.github/PULL_REQUEST_TEMPLATE.md` and add these sections:
   - **Plan** (from Phase 1).
   - **Closes #N** lines, one per story. Use "Part of #EPIC" for the epic,
     never "Closes #EPIC".
   - **Test changes** table. Required even if empty; see `test-integrity.md`.
   - **Data & cost**: the `data-and-cost.md` checklist, or "None — <reason>".
   - **Docs**: what was updated, or "None — <reason>".
   - **Assumptions**: implementation choices made without asking.
   - **Cost tier**: the tier used and why. Use tier names only, no model IDs.

   Add the story's labels (`epic:*`, plus `security` / `ci` / `documentation`
   as relevant) and the `cost:*` label. If a label doesn't exist yet, it is
   created when first applied.

2. **Codex.** If the Codex GitHub app is installed on this repo, post
   `@codex review` as a PR comment immediately and note the time. If it never
   reacts (no 👀 within a few minutes), note "Codex not available" and carry
   on. See `reviews.md`.
3. **Review loop.** Run the loop in `reviews.md`: keep going until only nits
   remain, with a maximum of 3 rounds before escalating. Every round and every
   response is a PR comment.
4. **Test-integrity audit.** Run the audit in `test-integrity.md`.
5. **Data and cost review.** Check the diff against the PR's Data & cost
   section. A migration, AI or catalog change the section doesn't declare is a
   blocking finding.
6. **Security review.** Run it if Phase 1 said so (`reviews.md`).
7. **Bring the branch up to date.** Merge `origin/dev` into the branch (don't
   rebase), then re-run the local checks.
8. **Mark the PR ready for review.**
   - BookshelfAI CI runs on drafts too, so CI results are already coming in
     during the loop. Treat red CI on the draft as review findings.
   - Mark ready only when the loop has converged, CI is green on the head
     commit, and Codex has responded, or 10 minutes have passed since the
     draft was opened.

## Phase 4 — CI, conflicts, hand-off

1. **Watch CI** on the head commit. Prefer `subscribe_pr_activity` (cloud) or
   a `Monitor` / `ScheduleWakeup` check-in (local). Never `sleep`-poll.
2. **Red CI.** Root-cause and fix it with the implementer tier, or one tier
   lower for mechanical fixes (lint, lockfiles, Podfile.lock).
   - Each fix gets a short PR comment naming the check, the cause and the
     commit.
   - A failure that is also red on `dev` isn't this PR's. Say so on the PR and
     file or link an issue.
   - Two consecutive failed fix attempts on the same check: escalate (owner
     issue, or one tier up per `model-selection.md`).
   - Native jobs (`ios-build-check`, `android-build-check`, `podfile-lock-check`,
     `gradle-wrapper-check`) follow `docs/claude/ios-ci.md` and
     `docs/claude/android-ci.md`.
3. **Late review comments** (Codex or human) after the PR is ready are handled
   exactly like a review round and documented on the PR.
4. **Merge conflict.** Merge `origin/dev` in and resolve with the cheapest
   tier that can (`coordination.md`). Re-run the local checks, then push.
   Regenerate lockfiles with the repo's tooling; never hand-edit them.
5. **Owner-attention labels.** Add `owner-approval-required` and a PR comment
   summarising exactly what to check when the PR touches any of:
   - a database migration (`backend/alembic/`)
   - deletion, export or retention of user data
   - auth, tokens or sessions
   - AI model choice, prompts, quotas, budgets or kill switches
   - subscriptions, IAP, entitlements or pricing
   - rate limits
   - Cloudflare or CORS configuration
   - outbound email (Resend)

   The owner merges every PR anyway. The label tells them which ones need a
   careful read rather than a skim.

6. **Hand-off.** A PR is ready for the owner when all of these hold:
   - CI is green on the head commit
   - there are no conflicts
   - there are no open blocking findings
   - Codex threads are answered and resolved

   Then post a short **Ready for merge** comment: what it does, what was
   reviewed, any manual QA for the owner, and the merge method (squash into
   `dev`). List the PR under **Needs you** in the report. Keep working other
   stories in the meantime.

7. **After the owner merges** (on the next wake or run):
   - Confirm each `Closes #N` issue closed. If not, close it with
     `state_reason: completed` and a comment linking the PR.
   - Tick the story in the epic's child list. If every child is closed, run
     the epic's success-metrics check and close the epic.
   - Delete the branch and worktree.
   - Watch the post-merge `dev` CI run. If the merge broke `dev`, that is now
     the top priority: fix forward fast, or open a revert PR.
   - Note any Render deploy or Alembic migration step the owner must run.

## Phase 5 — Report

End every run (and every long pause) with this report, in this order. The
first section is never omitted, even when empty:

```
## Needs you
- PR #142 ready to merge (owner-approval-required: migration adds user_book_formats)
- #123 decision: <one-line question> (blocks #124, #125)
- (or "Nothing — no open decisions or merges waiting.")

## Ready / merged
- #101 → PR #140 ready for merge (cost: standard) — <one line>
- #99 → PR #138 merged by owner; issue closed

## In flight
- #102 → PR #141, waiting on CI (ios-build-check)

## Filed during this run
- #150 bug: <title> (found in review of #140)

## Skipped
- #103 — claimed by another session (PR #139)
```
