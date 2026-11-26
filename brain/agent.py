"""handle_turn: the one entry point (tests, CLI and eval all call it). Caller identity, group, thread and
clock are arguments, never read from message text or the real clock."""
import asyncio
from dataclasses import dataclass, field

from langfuse import get_client, observe, propagate_attributes

from brain import config, graph, mcp_client
from brain.webex import WebexWorkflow, release_pending  # noqa: F401
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
                       toolbox=None, checkpoint_path=None, webex_port=None) -> TurnResult:
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
            cfg["configurable"]["webex_port"] = webex_port or WebexWorkflow(box)
            box.ctx = {"as_of": as_of, "conversation_id": thread_id, "request_id": request_id}  # application-set, never the model
            with propagate_attributes(session_id=thread_id, user_id=caller_id,
                                      metadata={"caller_group": caller_group, "request_id": request_id}):
                out = await g.ainvoke({"message": message}, cfg)
            r = TurnResult(out["answer"], out["status"], out["scope"], out["visible"],
                           [t["tool"] for t in out["tool_log"]], out["sources"], out["plan"]["intent"],
                           request_id, out["error"],
                           extra={"tool_calls": [t for t in out["tool_log"] if not t.get("rejected")],
                                  "evidence": out["evidence"], "handoff": out.get("handoff"), "checklist": out.get("checklist"),
                                  "handoff_calls": out.get("handoff_calls") or 0, "decision": out.get("decision"),
                                  "paused": bool(out.get("__interrupt__")), "pending": out.get("pending")})
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


async def aresume_pending(caller_id: str, caller_group: str, thread_id: str, as_of: str,
                          toolbox=None, checkpoint_path=None) -> TurnResult:
    """Resume a paused approval, possibly in a new process. The recorded approval rows decide: while any is not
    'approved' nothing is released and the checkpoint stays paused. Nothing in a resume message is ever read."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.types import Command
    path = checkpoint_path or CHECKPOINT_DB

    async def go(box):
        box.ctx = {"as_of": as_of, "conversation_id": thread_id, "request_id": f"{thread_id}:resume"}
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            g = graph.build_graph(saver)
            cfg = {"configurable": {"thread_id": thread_id, "toolbox": box, "as_of": as_of,
                                    "request_id": f"{thread_id}:resume"}}
            st = await g.aget_state(cfg)
            pend = {k: v for k, v in (st.values.get("pending") or {}).items() if not v["released"]}
            if not pend:
                return TurnResult("Nothing is pending on this conversation.", "completed", "webex", [], [], [], "access_request",
                                  cfg["configurable"]["request_id"])
            checks = {rid: await release_pending_check(box, p) for rid, p in pend.items()}
            if not all(checks.values()):  # rows not all approved: stay paused, write nothing
                text = " ".join([graph.resume_text(rid, {"released": False, "approvals": await _status(box, p)})
                                 for rid, p in pend.items()])
                return TurnResult(text, "pending", "webex", [], [], [], "access_request", cfg["configurable"]["request_id"])
            if st.next:  # the interrupt is still there: resume the graph itself
                out = await g.ainvoke(Command(resume="rows-recorded"), cfg)
                return TurnResult(out["answer"], out["status"], "webex", [], [], [], "access_request",
                                  cfg["configurable"]["request_id"], extra={"pending": out.get("pending")})
            # the interrupt was dropped by a later turn on this thread: same release, from the persisted pending record
            notes, new = [], dict(st.values["pending"])
            for rid, p in pend.items():
                r = await release_pending(box, p)
                new[rid] = {**p, "released": r["released"]}
                notes.append(graph.resume_text(rid, r))
            await g.aupdate_state(cfg, {"pending": new})
            return TurnResult(" ".join(notes), "needs_human", "webex", [], [], [], "access_request", cfg["configurable"]["request_id"],
                              extra={"pending": new})

    if toolbox is not None:
        return await go(toolbox)
    async with mcp_client.open_toolbox(caller_id) as box:
        return await go(box)


async def _status(box, p: dict) -> dict:
    return {a["approval_id"]: a["status"] for a in await box.call("read_approval", {"request_id": p["request_id"]})}


async def release_pending_check(box, p: dict) -> bool:
    s = await _status(box, p)
    return bool(s) and all(v == "approved" for v in s.values())


def resume_pending(*a, **kw) -> TurnResult:
    return asyncio.run(aresume_pending(*a, **kw))
