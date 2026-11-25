"""handle_turn: the one entry point (tests, CLI and eval all call it). Caller identity, group, thread and
clock are arguments, never read from message text or the real clock."""
import asyncio
from dataclasses import dataclass, field

from langfuse import get_client, observe, propagate_attributes

from brain import config, graph, mcp_client
from brain.scope import WRITE_TOOLS


@dataclass
class TurnResult:
    answer: str
    status: str
    scope: str
    visible_tools: list
    tool_sequence: list
    sources: list
    intent: str
    request_id: str
    error: str = ""
    extra: dict = field(default_factory=dict)


CHECKPOINT_DB = config.ROOT / "state" / "agent.db"


async def ahandle_turn(caller_id: str, caller_group: str, thread_id: str, message: str, as_of: str,
                       toolbox=None, checkpoint_path=None) -> TurnResult:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    path = checkpoint_path or CHECKPOINT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        g = graph.build_graph(saver)
        cfg = {"configurable": {"thread_id": thread_id}}
        n = len((await g.aget_state(cfg)).values.get("messages") or []) // 2 + 1
        request_id = f"{thread_id}:{n}"
        cfg["configurable"].update(request_id=request_id, as_of=as_of)

        @observe(name="agent.turn")
        async def run(box):
            cfg["configurable"]["toolbox"] = box
            with propagate_attributes(session_id=thread_id, user_id=caller_id,
                                      metadata={"caller_group": caller_group, "request_id": request_id}):
                out = await g.ainvoke({"message": message}, cfg)
            r = TurnResult(out["answer"], out["status"], out["scope"], out["visible"],
                           [t["tool"] for t in out["tool_log"]], out["sources"], out["plan"]["intent"],
                           request_id, out["error"])
            get_client().update_current_span(
                input=message, output=r.answer,
                metadata={"caller_id": caller_id, "caller_group": caller_group, "scope": r.scope,
                          "intent": r.intent, "visible_tools": r.visible_tools, "tool_sequence": r.tool_sequence,
                          "write_tools_visible": [t for t in r.visible_tools if t in WRITE_TOOLS],
                          "status": r.status, "request_id": request_id, "as_of": as_of, "sources": r.sources})
            return r

        if toolbox is not None:
            return await run(toolbox)
        async with mcp_client.open_toolbox(caller_id) as box:
            return await run(box)


def handle_turn(*a, **kw) -> TurnResult:
    return asyncio.run(ahandle_turn(*a, **kw))
