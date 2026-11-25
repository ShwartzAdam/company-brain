"""python scripts/ask.py --caller E010 "question"   (server must be running)
Step 1 answerer: retrieve through the MCP server, one llm.complete call, cited sources."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

from langfuse import observe  # noqa: E402

from brain import config, llm  # noqa: E402

SYSTEM = ("You are the NovaOps assistant. Answer ONLY from the numbered context. Cite sources as "
          "[n] with the file path. If the context lacks the answer, say so.")


async def retrieve(question: str, caller: str) -> list[dict]:
    async with streamablehttp_client(config.MCP_URL) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("search_documents", {"query": question, "caller_id": caller, "k": 5})
            return [json.loads(c.text) for c in res.content]


@observe(name="ask.answer")
def answer(question: str, hits: list[dict]) -> str:
    ctx = "\n\n".join(f"[{i}] {h['source']}\n{h['text']}" for i, h in enumerate(hits, 1))
    return llm.complete([{"role": "user", "content": f"Context:\n{ctx}\n\nQuestion: {question}"}], SYSTEM)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--caller", required=True)
    ap.add_argument("question")
    a = ap.parse_args()
    hits = asyncio.run(retrieve(a.question, a.caller))
    print(answer(a.question, hits))
    print("\nsources:", *sorted({h["source"] for h in hits}), sep="\n  ")
