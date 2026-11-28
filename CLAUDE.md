# CLAUDE.md: Company Brain (NovaOps final project)

Spec: `PROJECT-DESCRIPTION.md` in the course folder is the source of truth. Build from the spec,
not from open-ended prompting. Plan and findings: `docs/PLAN-VALIDATION.md`, `docs/KNOWN-TRAPS.md`.

## Rules the agent must follow
- Fixed stack: Bedrock (Nova 2 Lite), Langfuse, Python, tools behind an MCP server boundary.
- Every model call goes through `llm.py`. Nothing else imports a Bedrock client.
- The graph never touches the database. It calls MCP tools only.
- Only application code records that an action happened. A model may propose or explain.
- Writes are idempotent (employee + system + conversation id). Reuse `T001`, `AR001`, `AP001`, `AP002`.
- Exactly one write-release function. Maya's scope has no write tools.
- Caller identity and group are arguments, never read from message text.
- `manager_playbook` is restricted: filter at retrieval, never refuse after the fact.
- Missing `audience` means unrestricted. A manager is someone who is another employee's `manager_id`.
- Never use the real clock. Pass `as_of` (evaluation pins 2026-07-01).
- Terminal status is exactly one of completed, blocked, pending, needs_human, failed, and the answer must agree.

## Never commit
`.env`, keys, tokens, `data/` (restricted documents), `*.db`.

## Quality gates before every commit
1. `pytest` green. 2. `scripts/secret_scan.sh` clean. 3. README claims match code.
4. The milestone's "done when" is demonstrated, not assumed.

## Conventions
- One commit per milestone, message says what was verified.
- Our own checks live in `evals/` and are read as part of the submission.
- The lesson repo `ai-course` is read-only: read an asset, adapt a copy here.
