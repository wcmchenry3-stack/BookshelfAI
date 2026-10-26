# Model selection

The goal is that every piece of work runs on the cheapest model that will get
it right the first time. A cheap model that needs three CI rounds and a
re-review isn't cheap, so pick honestly. Default low, and escalate on
evidence.

The orchestrator session plans, reviews diffs and hands off PRs.
Implementation, conflict resolution and most review rounds go to subagents
through the `Agent` tool's `model` parameter.

These are **the models that build the app**. They have nothing to do with the
models the app calls at runtime, which are chosen in the AI gateway registry
(`data-and-cost.md`).

## Tiers

| Cost label      | `model`  | Use for                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| --------------- | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cost:low`      | `haiku`  | Mechanical, fully specified work. Examples: docs-only changes, i18n string additions across `frontend/locales/*`, config or label tweaks, renames, lint or format fixes, lockfile or docs merge conflicts, tests for a pure function whose expected outputs the story gives.                                                                                                                                                                                                                                                                                                     |
| `cost:standard` | `sonnet` | **The default.** A feature or bug inside one screen or one backend module with clear ACs. Examples: UI components and screens, new read endpoints following an existing pattern, enrichment/provider parsing, most bug fixes with a known or findable root cause, most review rounds, code-level merge conflicts.                                                                                                                                                                                                                                                                |
| `cost:high`     | `opus`   | Cross-cutting or risky work. Examples: <br>• anything in `data-and-cost.md` beyond "none": migrations, user-data deletion or export, shared-catalog writes, AI gateway, quotas, kill switches <br>• auth, JWT, refresh tokens, Sign in with Apple <br>• subscriptions, IAP, entitlements <br>• rate limiting and client-IP handling <br>• the upload/image pipeline <br>• native iOS/Android build config (`frontend/ios/`, `frontend/android/`, `ci_scripts/`) <br>• concurrency or race bugs whose root cause is unknown <br>• changes spanning frontend + backend + migration |
| `cost:max`      | `fable`  | Only after the split check below fails. Examples: a root cause an Opus attempt investigated and couldn't find, or an architectural change that genuinely can't be staged.                                                                                                                                                                                                                                                                                                                                                                                                        |

**Signals that push a story down a tier:**

- The ACs list exact inputs and outputs.
- There's an existing sibling to copy (another endpoint, screen or test of
  the same shape).
- The diff is expected to be under ~150 lines in 1–3 files.
- No `data-and-cost.md` line applies.

**Signals that push a story up a tier:**

- "investigate" or "unknown root cause"
- It touches money, auth, user data, the shared catalog or AI spend.
- It touches `frontend/ios/`, `frontend/android/`, Podfile or Gradle.
- A previous attempt on this story failed.
- The ACs are about judgement (recognition quality, ranking, recommendations).

## The Fable gate

Before assigning `fable`, write these three things in the PR's **Cost tier**
section (or in the decision issue, if you stop instead):

1. **Split attempt.** How you tried to split it and why each split doesn't
   work. Typical splits:
   - a prep refactor first, then the behaviour change
   - expand migration, then feature, then contract
   - backend separately from UI
   - a spike issue to find the root cause, then a fix story
2. **Why Opus isn't enough.** Be concrete: an Opus attempt failed (link it), or
   the problem has a property Opus is known to struggle with here.
3. **Scope.** Fable does only the hard core. The surrounding mechanical work
   (edge-case tests it specifies, docs, wiring, conflict fixes, nit-level
   review follow-ups) goes back to Sonnet or Haiku.

If you split instead, file the sub-issues (plan-issues conventions) and ship
them individually. Each piece usually lands at `standard` or `high`.

## Escalation ladder

Start at the chosen tier. Move **up one tier** when, on the same story:

- the implementer reports it couldn't satisfy an AC, or
- two consecutive review rounds return the same blocking finding unfixed, or
- two consecutive CI fix attempts fail on the same check for a reason in this
  PR's code.

Record each escalation as a PR comment ("escalated standard → high:
<reason>") and update the `cost:*` label to the highest tier used. If you're
already at `high` and the Fable gate isn't met, escalate to the owner instead
(decision issue).

Move **down** for follow-up work whenever possible. Once a high or max-tier PR
is open, nits, docs, conflict resolution and lint fixes go to `sonnet` or
`haiku`.

## Reviewers

- **Review rounds:** `sonnet` for `cost:low` and `cost:standard` PRs, `opus`
  for `cost:high` and `cost:max` PRs. A reviewer is never cheaper than
  `sonnet`.
- **Security review:** `opus` whenever the PR touches auth, entitlements, IAP,
  user data, the shared catalog, the AI gateway, or the upload pipeline.
  Otherwise `sonnet`.
- The orchestrator still reads every diff once before marking ready.
