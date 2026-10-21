# Reviews

There are three review sources: our code-review rounds, the Codex GitHub app
(if installed), and a security review, which most backend PRs here trigger.
All of them happen while the PR is a **draft**. Every round and our response
to it is recorded as a PR comment, so the PR is the full record.

## Code-review loop

A reviewer subagent runs each round, at the tier set in `model-selection.md`.
It uses the `code-review` skill on the PR at `high` effort, plus the
test-integrity audit and the data-and-cost check. The reviewer must not be
the same agent instance that wrote the code.

For each round:

1. The reviewer returns findings, each with a severity:
   - **blocking** — any of:
     - a correctness bug or a missing AC
     - a missing or weak test
     - a security issue
     - data loss or corruption
     - a crash
     - platform breakage (iOS/Android first)
     - a test-integrity violation
     - an undeclared migration, AI call or catalog write
     - a client-trusted value (see `data-and-cost.md`)
     - a missing rate-limit decorator, hardcoded string or hardcoded colour
     - a doc that is now wrong
   - **should-fix** — real but not critical: an edge case, error handling,
     a11y, an i18n gap, performance on a hot path, a missing log field or an
     over-logged one.
   - **nit** — style, naming, comments, micro-refactors.
2. Post the round as one PR comment (format below).
3. Fix every blocking and should-fix finding, at the implementer tier or lower
   for small fixes.
   - **Nits:** fix the cheap ones in the same push and decline the rest with a
     one-line reason.
   - **A finding you disagree with** is answered, not ignored: say why it isn't
     a bug, with the code path you traced.
4. Bugs that are real but **outside this PR's scope** get an issue
   (`escalation.md`). Link it in the response and don't fix it here.
5. Post the response comment, push, and start the next round on the new head.

**Stop** when a round returns no blocking or should-fix findings. **Cap:** if
round 3 still has blocking findings, don't run round 4. Escalate instead: one
tier up, or an owner decision if the disagreement is about intended
behaviour. Repeated findings of the same kind mean you fix the root cause,
not the instance.

### Round comment format

```
### Review round 2 — code review (reviewer tier: standard)
| # | Severity | File:line | Finding |
| - | -------- | --------- | ------- |
| 1 | blocking | backend/app/api/user_books.py:118 | edition_id not checked against book_id; mismatched pair returns 500 |
| 2 | nit | ... | ... |

Test-integrity audit: no existing tests modified. OK.
Data & cost: matches declared (additive migration, no AI calls). OK.
Codex: no comments yet (opened 6 min ago).
```

### Response comment format

```
### Response to round 2
| # | Disposition | Detail |
| - | ----------- | ------ |
| 1 | fixed | a1b2c3d — validate pair, 422 on mismatch; regression test added |
| 2 | declined | naming matches sibling routers (wishlist, purchased) |
| 3 | filed #512 | pre-existing: refresh endpoint allows concurrent reuse |
```

## Codex

The Codex app has not been confirmed as installed on this repo. On the first
PR of a run, post `@codex review`. If there is no 👀 reaction within about 5
minutes, assume it isn't installed. Write "Codex: not available" in the round
comments for the rest of the run and don't wait on it.

When it is available:

- **Timing.** Give Codex up to **10 minutes** from when the draft was opened.
  Don't idle while you wait; the first code-review round runs during that
  window. A 👍 reaction means it responded with no findings.
- **Second review.** Marking the PR ready triggers a second Codex review
  automatically. Treat it like any late review: triage and answer before
  hand-off.
- **Findings.** Triage them exactly like a round's findings and answer them in
  the next response comment, numbered `C1`, `C2` and so on. Reply on each
  Codex inline thread with the disposition and resolve it.
- **Codex "Summary" comments.** These describe a fix Codex made in its own
  environment; nothing was pushed to the branch. Treat the summary as a
  finding: apply the parts that hold up and say which parts you didn't take,
  and why.
- **Security reviews.** When the PR needs one, also comment
  `@codex security review` alongside running our own.

## Security review

Run the `security-review` skill, at the tier set in `model-selection.md`,
after the code-review loop converges and before marking the PR ready. Run it
when the diff touches any of:

- **Auth and identity:** Google sign-in, Sign in with Apple, JWT
  signing/verification, refresh tokens, sessions, account deletion or export.
- **Payments:** subscriptions, IAP, receipts, entitlements, pricing,
  tier/quota enforcement.
- **AI:** the gateway, prompts, model registry, structured-output parsing,
  handling of model output.
- **Uploads:** the image pipeline (`core/file_validation.py`,
  `core/file_security.py`, `/scan`), file size or type limits.
- **Endpoints:** new or changed HTTP endpoints, request validation, rate
  limits, the client-IP / Cloudflare / TrustedHost / CORS / security-header
  middleware.
- **Data:** DB queries or migrations that take user input, or that delete or
  alter user data; writes to the shared catalog.
- **Outbound calls:** Open Library, Google Books, Turnstile, Resend, Sentry
  (SSRF, timeouts, keys).
- **Personal data:** collection, logging of identifiers or emails, Sentry
  scrubbing, the privacy policy.
- **Client security:** network client, token storage (`SecureStore`), deep
  links, WebViews.
- **Dependencies:** new third-party dependencies or native modules.
- **Supply chain:** secrets, env vars, `render.yaml`, CI workflows, GitHub
  Actions permissions.

Skip it only for: UI layout and styling, copy and i18n strings, docs-only, and
test-only changes. If unsure, run it. It is cheap compared with a shipped
vulnerability or a runaway AI bill.

Post it as its own comment (`### Security review`), with findings and
dispositions in the same table format. Security findings are never "nit".
Each one is either fixed or explicitly accepted by the owner through a
decision issue.

## Before hand-off

Run the repo's `pytest -m security` suite locally whenever the PR touches
backend code (CI runs it only on some paths). Report the result in the final
round comment.
