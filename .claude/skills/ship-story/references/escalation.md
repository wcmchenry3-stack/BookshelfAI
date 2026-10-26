# Escalation: owner decisions, owner actions, and unrelated bugs

The failure mode this prevents: a question gets mentioned once in chat, work
continues, and the question is 20 messages back when the owner looks. So:
**anything the owner must see becomes a GitHub issue, and every report
starts with a "Needs you" list.** Chat is never the only place a question
lives.

## When you need a decision

Decide yourself (and list it under **Assumptions** in the PR) when it's an
implementation detail: naming, file layout, test structure, which existing
pattern to follow, error message wording that follows existing copy.

Escalate when it's any of:

- Product behavior the ACs don't specify, or ACs that conflict.
- Which platform(s) a story targets, when unstated and it matters.
- Monetization, pricing, tier limits, store listing, legal/privacy.
- Destructive or irreversible data changes (migrations that drop/alter user
  data, hard deletes), or changes to how the shared catalog is written.
- AI model choice, prompt changes that affect recognition quality, quota or
  budget numbers, or anything that raises per-user AI cost.
- Anything that raises infrastructure cost (Supabase plan/storage, Render,
  Sentry volume, Resend volume) without a cap and a dial.
- An existing test whose expectation you believe is wrong (test-integrity.md).
- A security finding you'd accept rather than fix.
- Merge conflicts where both sides changed the same behavior differently.
- Anything needing access, credentials, accounts, or a device you don't have.

### How

1. Search for an existing open decision issue on the same question first.
2. Create an issue labeled `blocked:owner-decision`:

   ```
   Title: Decision needed: <question in one line>

   ## Question
   <one or two sentences>

   ## Context
   Story #123, PR #140 (if any). What you found, with file:line links.

   ## Options
   1. <option> — consequence
   2. <option> — consequence

   ## Recommendation
   <option N, and why>

   ## Blocks
   #123, #124 (and their dependents). Unblocked work continues meanwhile.
   ```

3. Comment on each blocked story linking the decision issue, and add the
   `blocked:owner-decision` label to it. Release the claim if you're stopping.
4. If the session has `PushNotification`, send one short notification.
5. **Stop work on what it blocks.** Continue with every story it doesn't
   block. Don't build on a guessed answer.
6. Include it in the **Needs you** section of every report until it's
   answered.

When the owner answers (on the issue or in chat), record the answer on the
issue, remove the label from the issue and the stories, close the decision
issue, and resume.

## When the owner must _do_ something

Things only the owner can do — device QA on a physical phone, App Store
Connect / Play Console steps, adding a secret, rotating a key, Supabase or
Render dashboard changes, Cloudflare rules, provider spend limits, granting
access — get an issue too (label `ops` or `qa` plus `blocked:owner-decision`
only if work is actually blocked on it). Don't block merging to `dev` on
device QA unless the story says so; list it in the PR under **Manual QA for
owner** and file the issue.

## Unrelated bugs found along the way

Bugs found in review, while reading code, or from a CI failure that isn't this
PR's:

1. Search open issues for duplicates; if one exists, add a comment with the
   new evidence instead.
2. File a `bug` issue using the bug template in `.claude/agents/plan-issues.md`
   (reproduction, evidence, suspected root cause with file:line, impact), plus
   area labels. Mention where it was found ("found during review of PR #140").
3. Link it from the PR's review response and the run report.
4. Don't fix it in the current PR unless it blocks the story's ACs. If it
   does block, say so in the PR and keep the fix minimal.
