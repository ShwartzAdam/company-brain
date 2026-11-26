"""Tiny runner so the restart test can use two real, separate processes on the same files.
  python tests/_webex_proc.py start   <ck.db>   pauses Rachel's Webex request, then the process exits
  python tests/_webex_proc.py resume  <ck.db>   a fresh process reads the approvals and resumes
The database comes from BRAIN_DB_PATH (set by the test)."""
import asyncio
import json
import os
import sys
from pathlib import Path

os.environ["LANGFUSE_TRACING_ENABLED"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from mcp.shared.memory import create_connected_server_and_client_session as connect  # noqa: E402

from brain import agent, llm  # noqa: E402
from brain.mcp_client import Toolbox  # noqa: E402
from server.server import mcp  # noqa: E402

AS_OF = "2026-07-01T12:00:00Z"
REPLIES = [json.dumps({"intent": "access_request", "requires_tools": True, "system_name": "Webex"}),
           '{"tool":"check_software_subscription","args":{"software":"Webex"}}', '{"done":true}', "ok"]


class Fake:
    def __init__(self):
        self.q = list(REPLIES)

    def complete(self, messages, system, max_tokens, temperature):
        return self.q.pop(0) if len(self.q) > 1 else self.q[0]


async def main(mode, ck):
    llm.set_backend(Fake())
    from brain import retrieval
    retrieval.search = lambda *a, **k: [{"source": "documents/it_kb/webex_login.md", "text": "t", "section": "s", "audience": "all", "score": 1}]
    async with connect(mcp._mcp_server) as s:
        box = await Toolbox(s, "E010").load()
        if mode == "start":
            r = await agent.ahandle_turn("E010", "UG_REGULAR", "w-i-03", "I joined Customer Success and need Webex access. "
                                         "Webex says my account is not licensed.", AS_OF, toolbox=box, checkpoint_path=Path(ck))
        else:
            r = await agent.aresume_pending("E010", "UG_REGULAR", "w-i-03", AS_OF, toolbox=box, checkpoint_path=Path(ck))
        print(json.dumps({"status": r.status, "answer": r.answer, "paused": r.extra.get("paused")}))

asyncio.run(main(*sys.argv[1:3]))
