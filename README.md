# Company Brain (NovaOps final project)

Status: Step 1 (foundation) built: ingest with `audience`, SQLite from the dataset, MCP server, one model-call module. No agent graph yet (Step 2). Nothing below is claimed unless it exists in this repo.

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
`llm.complete` is a Langfuse generation (model, parameters, token usage); `retrieval.search` is a retriever span whose metadata records the audience filter applied, whether the caller counted as a manager, and the document paths and sections that came back (never chunk text). The rerank call nests under the search. The MCP server runs in its own process, so its retrieval traces are separate from the client's `ask.answer` trace until Step 2 passes a trace id. Tests set `LANGFUSE_TRACING_ENABLED=false`.

## Decisions
See also `PLAN-VALIDATION.md` and `KNOWN-TRAPS.md`.
- **Audience rule.** Only `handbook` (`all`) and `manager_playbook` (`manager`) carry `audience`. A document with none is stored as `all` (unrestricted). Ingest fails on any other value, or on a playbook file not marked `manager`.
- **Manager rule.** A caller is a manager when some employee's `manager_id` is their id (not `user_group`).
- **Filter at retrieval.** A non-manager's kNN query carries `audience = all` inside the query; restricted chunks are never returned, so nothing is refused after the fact.
- `caller_id` is a tool argument supplied by the application. Writes: the caller must be the subject, a manager, or in the HR/IT group.
- Not yet built: agent graph, scope-based tool visibility, consent gate, Langfuse tracing, `as_of` plumbing beyond the write tools.

## Step 1 measurements (live, 2026-11-25)
- 102 documents, 740 chunks indexed. `evals/leak_check.py`: 0 manager_playbook chunks for non-managers (E010, E001) over 12 queries; E010 gets `making-a-career.md`; E018 reaches the playbook.
- Retrieval: vector + BM25 (30 candidates each), fused by reciprocal rank, reranked by Nova to 5. The audience filter is in both queries; the reranker only sees filtered chunks. `evals/retrieval_check.py` scores documents only (database tables are not retrievable here; three cases need the agent's second search and are listed, not scored). Mean recall@5, 5 `retrieval_expectations` / 16 `single_turn_cases`: vector 0.53 / 0.71, hybrid 0.60 / 0.62, hybrid + rerank 0.70 / 0.89. A pool of 50 was worse (0.68 / 0.79), so 30 stays. Remaining misses are general policies (`access_management_policy`, `onboarding_policy`) outranked by person- or product-specific files; the agent loop's follow-up searches are the next lever. `evals/diagnose.py` shows each miss's rank.
- S1-quick-policy turn 1 via `scripts/ask.py --caller E010`: answers "five business days" with a cited `policies/equipment_policy.md`.
