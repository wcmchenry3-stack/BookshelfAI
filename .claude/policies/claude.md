# Claude / Anthropic API Policy

Policy references:

- https://www.anthropic.com/policies/aup
- https://docs.anthropic.com/en/docs/about-claude/models
- https://docs.anthropic.com/en/api/rate-limits
- https://docs.anthropic.com/en/api/errors
- https://docs.anthropic.com/en/api/messages

Enforced in CI by `scripts/policy/check_claude_policy.py` (the `claude-policy`
job in `.github/workflows/ci.yml`). Fixture tests:
`backend/tests/unit/test_claude_policy.py`.

## Detection

Files trigger this policy when ANY of these is true:

- the path is under `app/ai/` (the AI gateway and its providers);
- the content matches `from anthropic|import anthropic|@anthropic-ai/sdk|Anthropic\(|ANTHROPIC_API_KEY|claude_agent_sdk|\.messages\.(\w+\.)*(create|stream)\(` (the last one catches an injected client calling the SDK outside `app/ai/`).

The policy-gate hook tests the detect pattern against the file path as well as its contents.

Skip `.env` / `.env.*` files (variable declarations, not logic). Tests, docs and
`.claude/` are not scanned.

"SDK call" below means any `...messages...create(` / `...stream(` chain (including
`messages.with_raw_response.create`, `messages.batches.create`, `beta.messages.create`),
`AsyncAnthropic(` or `Anthropic(`.

## Checks

### Check 1: API key not hardcoded [error]

**Pattern**: `sk-ant-[a-zA-Z0-9\-_]{20,}`
**Presence**: forbidden
**Message**: Hardcoded Anthropic API key detected. Store keys in environment variables and access via `os.environ` or `process.env`.
**Reference**: https://docs.anthropic.com/en/api/getting-started

### Check 2: Retry on 429, 500, 503, 529, honouring retry-after [error]

Code-level (AST) check: comments and docstrings never count.

**Applies to**: files containing an SDK call
**Pattern**: each of `429`, `500`, `503`, `529` (overloaded) must be handled, and `retry-after` must be respected
**Presence**: required
**Message**: Anthropic returns 429 (rate limit), 500, 503 and 529 (overloaded). Retry them with bounded exponential backoff and never wait less than the `retry-after` header.
**Reference**: https://docs.anthropic.com/en/api/errors

### Check 3: Model IDs pinned to the approved list [error]

**Pattern**: quoted `claude-...` strings
**PASS**: an ID in the approved list (`backend/app/ai/registry.py` `PROVIDER_MODELS` /
`backend/app/ai/models_allowlist.py` `ALLOWED_MODELS["anthropic"]`; today `claude-haiku-5-5`
and `claude-sonnet-5-5`), or a dated snapshot ID (ends `-YYYYMMDD`).
**FAIL**: a moving alias (`*-latest`, for example `claude-3-5-sonnet-latest`) or any Claude ID
that is not in the approved list and is not a dated snapshot.
**Note**: Claude 4.6+ and 5.x models have no dated snapshot. Their plain IDs are fixed IDs,
not moving aliases, so `claude-haiku-5-5` and `claude-sonnet-5-5` are correct. Adding a model
means adding it to `ALLOWED_MODELS` (and the registry) first.
**Reference**: https://docs.anthropic.com/en/docs/about-claude/models

### Check 4: max_tokens on every SDK call [error]

**Applies to**: `messages.create` / `messages.stream` calls
**Presence**: required (a `max_tokens=` keyword, or a `**request` whose dict sets `"max_tokens"`)
**Message**: The Messages API requires `max_tokens`; the server sets it from the feature spec, never the client.
**Reference**: https://docs.anthropic.com/en/api/messages

### Check 5: stop_reason / refusal handled [error]

Code-level (AST) check: a `stop_reason` identifier/attribute or the string `"refusal"` in executable code.

**Applies to**: files containing an SDK call
**Presence**: required (`stop_reason` is read and `refusal` is handled)
**Message**: A reply can end in `refusal` or `max_tokens`. Treat a refusal as an empty result, not as text to parse.

### Check 6: SDK calls only under `backend/app/ai/providers/` [error]

**Pattern**: `messages.create(`, `messages.stream(`, `AsyncAnthropic(`, `Anthropic(`
**Presence**: forbidden outside `backend/app/ai/providers/`
**Message**: Every model call goes through the AI gateway. Routers and services must not call the SDK.

### Check 7: No raw content in logs under `app/ai/` [error]

Code-level (AST) check. A bare raw-field name or an attribute named like one (`response.content`) is flagged; `image.width`, `raw.status_code`, `*_tokens` are not.

**Applies to**: Python logging calls (`logger.*`, `log.*`, `logging.*`) in `app/ai/`
**Forbidden arguments**: raw content fields (`text`, `content`, `raw`, `output`, `completion`, `reply`, image bytes, ...). Wrap in `len()` or a hash to log size or identity.
**Message**: Never log raw model output, prompts or uploaded image bytes.

### Check 8: Budget-breaker refusal is 503 `ai_budget_reached` [error]

**Check**: code-level (AST) over all of `backend/app/**/*.py`: an `HTTPException` whose `detail` is `"ai_budget_reached"` must have `status_code` 503 or `status.HTTP_503_SERVICE_UNAVAILABLE`
**Message**: The cost breaker refuses with `503 ai_budget_reached`.

## Known limits

The checker does not see aliased log methods (`warn = logger.warning`), model IDs built by
concatenation or kept in non-code files (JSON, YAML, env), or SDK use in non-Python files beyond a
text match. The runtime `ALLOWED_MODELS` validation (settings load and registry build) is the real
gate for model IDs; this check is a second line of defence.

The budget check (8) covers `HTTPException`-style calls with a literal `detail`; `JSONResponse`,
custom exception handlers and f-string or variable details are not seen. For `**request`
splats, check 4 resolves the dict bound to the name in the same file (a dict literal, `dict(...)`,
or the return value of a same-file function); anything it cannot resolve fails closed.

## Auto-Fix Guidance

- **Model pinning**: Replace a moving alias or unlisted ID with an ID from `ALLOWED_MODELS["anthropic"]`. Do not invent IDs and do not edit the allowlist to make a check pass; that is an owner decision.
- **Max tokens**: Set `max_tokens` from the feature spec (`spec.max_tokens`); do not hardcode a default.
- **Rate limiting / retries**: Flag for human review. Follow the loop in `backend/app/ai/providers/anthropic_vision.py`.
- **SDK location**: Move the call into a provider under `backend/app/ai/providers/` and call it through the gateway. Flag for human review.
- **Logging**: Replace the raw field with `len(...)` or a SHA-256 digest.
- **Hardcoded keys**: NEVER auto-fix by moving the key. Flag immediately; the key may be compromised and should be rotated.
