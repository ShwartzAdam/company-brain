# Company Brain (NovaOps final project)

Status: Step 0 (setup). Nothing below is claimed unless it exists in this repo.

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

## Decisions
See `PLAN-VALIDATION.md` and `KNOWN-TRAPS.md`.
