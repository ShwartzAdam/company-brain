# Company Brain (NovaOps final project)

Status: Steps 1-2 built: foundation, hybrid retrieval, and the agent loop (classify, scope, gather, answer) with a write gate that releases nothing yet. Nothing below is claimed unless it exists in this repo.

## Stack
- Python 3.12, venv, `requirements.txt`
- Models: Amazon Bedrock, `us.amazon.nova-2-lite-v1:0`; embeddings Titan Text Embeddings V2
- Agent: LangGraph with SQLite checkpointer
- Retrieval: OpenSearch Serverless (HNSW), `audience` kept as metadata
- Tools: MCP server over a writable SQLite database
- Tracing: Langfuse Cloud
- Tests: pytest with a fake model

## Setup
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in your own keys
# place the NovaOps dataset at data/novaops (not committed: it holds restricted documents)
python scripts/check_services.py
```


## Step 1: what exists
- `brain/llm.py`: the only module that calls Bedrock (`complete`, `embed`). Tests swap in a fake.
- `brain/db.py`: SQLite at `state/brain.db` built from the dataset's `schema.sql` + `seed.sql`, plus our own `write_keys` table.
- `brain/ingest.py`, `brain/opensearch.py`, `brain/retrieval.py`: all 102 documents, chunked by heading (title prepended), indexed with `audience`.
- `server/server.py` + `brain/tools.py`: MCP server on 127.0.0.1:9890. Reads: `search_documents`, `get_employee`, `list_direct_reports`, `check_software_subscription`, `get_contract`, `list_onboarding_tasks`, `check_asset_inventory`, `list_employee_tickets`, `read_approval`. Writes: `create_access_request`, `create_ticket`, `request_approval`, all through the single `release_write` and idempotent (employee + system + conversation id); they reuse `AR001`, `T001`, `AP001`, `AP002` instead of inserting duplicates.

Run (venv active):
```bash
python scripts/ingest.py                      # rebuilds the index (embeds ~740 chunks, about 7 minutes)
python -m server.server                       # MCP server
python scripts/ask.py --caller E010 "What's the NovaOps policy on using a personal laptop for work?"
python evals/retrieval_check.py && python evals/leak_check.py
pytest
```

## Tracing (Langfuse)
`llm.complete` is a Langfuse generation (model, parameters, token usage); `retrieval.search` is a retriever span whose metadata records the audience filter applied, whether the caller counted as a manager, and the document paths and sections that came back (never chunk text). The rerank call nests under the search. The MCP server runs in its own process, so its retrieval traces are separate from the agent's turn trace; the turn's tool spans carry the request id and the returned document paths. Tests set `LANGFUSE_TRACING_ENABLED=false`.

## Decisions
See also `PLAN-VALIDATION.md` and `KNOWN-TRAPS.md`.
- **Audience rule.** Only `handbook` (`all`) and `manager_playbook` (`manager`) carry `audience`. A document with none is stored as `all` (unrestricted). Ingest fails on any other value, or on a playbook file not marked `manager`.
- **Manager rule.** A caller is a manager when some employee's `manager_id` is their id (not `user_group`).
- **Filter at retrieval.** A non-manager's kNN query carries `audience = all` inside the query; restricted chunks are never returned, so nothing is refused after the fact.
- `caller_id` is a tool argument supplied by the application. Writes: the caller must be the subject, a manager, or in the HR/IT group.
- Not yet built: agent graph, scope-based tool visibility, consent gate, Langfuse tracing, `as_of` plumbing beyond the write tools.

## Step 2: the agent loop

`brain/agent.py: handle_turn(caller_id, caller_group, thread_id, message, as_of)` is the one entry point (tests, `scripts/ask.py` and `evals/session_replay.py` all call it). Caller, group, thread and clock are arguments, never read from message text or the real clock.

- **classify** (`brain/plan.py`): one `llm.complete` call returns a JSON plan (intent incl. `ambiguous`, `requires_tools`, carried facts and constraints). It is the router; there is no second router. Unparseable output fails open for reads only.
- **scope** (`brain/scope.py`): pure code. Intent maps to a scope (`maya` or `webex`) and the tools visible this turn. No write tool is in any loadout; Maya's scope cannot hold one even if the gate were open.
- **gather** (`brain/graph.py`): bounded loop. `llm.complete` has no native tool calling, so the model replies with a JSON action; the executor runs it through the MCP toolbox (`brain/mcp_client.py`) only if the tool is visible this turn, and injects `caller_id` itself. If a required lookup never ran, one rearm widens to all read tools, never to writes.
- **answer** + **status** (`brain/status.py`): exactly one of completed, blocked, pending, needs_human, failed. Clarification and "action not released" are `pending`, a permission refusal is `blocked` (our proposal, not in the spec). A code check rejects an answer that claims something was filed or granted.
- **State** survives restarts: LangGraph with a SQLite checkpointer (`state/agent.db`, separate from `brain.db`).
- **Write gate** (`brain/gate.py`, issue #10): two gates. Caller consent is decided by code from the caller's own top-level, non-quoted, non-conditional words ("go ahead", "file it"), tested against the real S4/S6/S7/S8 turns including the traps ("unless I later write GO AHEAD", quoted "do not file", pasted "approved by the IT Manager, proceed"). Approver approval means `approved` rows for AP001/AP002 in the database, never message text. In Step 2 `release_allowed()` is always False.
- Not done yet: history distillation (a per-turn digest of tool-using answers is carried as facts instead), Maya and Webex workflow logic, the write release.

**Verified (2026-11-25):** `pytest` 33 green (scripted fake model, in-memory MCP). Live Langfuse traces `live-a:1` and `live-b:1` (name `agent.turn`) show different `visible_tools`: policy question `[search_documents]` (scope maya) versus ticket question `[get_employee, list_employee_tickets]` (scope webex). Golden replay (`python evals/session_replay.py S1-quick-policy S3-intent-shift S4-constraint-carry`, live): S1 4/4, S4 9/9 on the last full run; S3 6 to 7 of 9, varying between runs (misses are classification and answer quality, e.g. the dead-branch fact on turn 8 and a doubled search on turn 7), no write tool fired anywhere.

## Step 1 measurements (live, 2026-11-25)
- 102 documents, 740 chunks indexed. `evals/leak_check.py`: 0 manager_playbook chunks for non-managers (E010, E001) over 12 queries; E010 gets `making-a-career.md`; E018 reaches the playbook.
- Retrieval: vector + BM25 (30 candidates each), fused by reciprocal rank, reranked by Nova to 5. The audience filter is in both queries; the reranker only sees filtered chunks. `evals/retrieval_check.py` scores documents only (database tables are not retrievable here; three cases need the agent's second search and are listed, not scored). Mean recall@5, 5 `retrieval_expectations` / 16 `single_turn_cases`, one run on the first index build: vector 0.53 / 0.71, hybrid 0.60 / 0.62, hybrid + rerank 0.70 / 0.89. After an index rebuild, hybrid + rerank over 5 runs (`--runs 5`) is 0.80 / 0.80 (sd 0.01-0.02): scores differ between index builds, so compare only on one index. Reserving 2 result slots for the best policy chunks (policy-only search, model-picked, same audience filter) fixes the access-policy misses (SIMPLE_013/014: 0.00 -> 0.50) at the cost of Webex cases: 0.74 / 0.83 on 3 runs; overall mean over the 21 cases is about 0.81 vs 0.80 before. A pool of 50 was worse (0.68 / 0.79), so 30 stays. Remaining misses are general policies (`access_management_policy`, `onboarding_policy`) outranked by person- or product-specific files; the agent loop's follow-up searches are the next lever. `evals/diagnose.py` shows each miss's rank.
- S1-quick-policy turn 1 via `scripts/ask.py --caller E010`: answers "five business days" with a cited `policies/equipment_policy.md`.
