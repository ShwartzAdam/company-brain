"""handle_turn: the one entry point (tests, CLI and eval all call it). Caller identity, group, thread and
clock are arguments, never read from message text or the real clock."""
import asyncio
import json
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
    trace_id: str = ""   # the Langfuse trace of this turn: the id the submission index lists


FAILED_ANSWER = "I couldn't finish this request because of an internal error or timeout, so nothing was done. Please try again."
CHECKPOINT_DB = config.ROOT / "state" / "agent.db"


def digest(out: dict, limit: int = 1500) -> dict:
    """What the agent DID, copied onto the root so one object tells the story (and a Langfuse-side judge can read it).
    Truncated, so strictly worse evidence than the trace's own tool spans."""
    return {"intent": out["plan"]["intent"], "requires_tools": out["plan"].get("requires_tools"),
            "tools_available": out["visible"],
            "state": {"facts": (out.get("facts") or [])[-12:], "constraints": out.get("constraints") or []},
            "calls": [{"tool": e["tool"], "args": e["args"], "result": json.dumps(e["result"], default=str)[:limit]}
                      for e in out["evidence"]]}


async def ahandle_turn(caller_id: str, caller_group: str, thread_id: str, message: str, as_of: str,
                       toolbox=None, checkpoint_path=None, webex_port=None, case_id: str = "", turn: int | None = None,
                       environment: str | None = None) -> TurnResult:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    path = checkpoint_path or CHECKPOINT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        g = graph.build_graph(saver)
        cfg = {"configurable": {"thread_id": thread_id}}
        n = len((await g.aget_state(cfg)).values.get("messages") or []) // 2 + 1
        request_id = f"{thread_id}:{n}"
        cfg["configurable"].update(request_id=request_id, as_of=as_of)

        @observe(name="agent.turn", as_type="agent", capture_input=False, capture_output=False)
        async def run(box):
            cfg["configurable"]["toolbox"] = box
            cfg["configurable"]["webex_port"] = webex_port or WebexWorkflow(box)
            box.ctx = {"as_of": as_of, "conversation_id": thread_id, "request_id": request_id}  # application-set, never the model
            lf = get_client()
            identity = {"caller_id": caller_id, "caller_group": caller_group, "request_id": request_id, "as_of": as_of,
                        "case_id": case_id or thread_id, "turn": str(turn if turn is not None else n)}
            lf.update_current_span(input=message, metadata=identity)  # at the START: a crashed turn is still attributable
            trace_id = lf.get_current_trace_id() or ""
            box.ctx["trace_id"] = trace_id
            try:
                with propagate_attributes(session_id=thread_id, user_id=caller_id, metadata=identity, environment=environment,
                                          tags=["novaops", "turn"]):
                    out = await asyncio.wait_for(g.ainvoke({"message": message}, cfg), config.TURN_TIMEOUT_S)
            except Exception as e:  # noqa: BLE001  (a timeout is one too): a turn never raises, it ends `failed`
                why = (f"turn timed out after {config.TURN_TIMEOUT_S:g}s" if isinstance(e, asyncio.TimeoutError)
                       else f"{type(e).__name__}: {e}")[:300]
                lf.update_current_span(output=f"failed: {why}", level="ERROR", status_message=why,
                                       metadata={**identity, "status": "failed", "error": why})
                return TurnResult(FAILED_ANSWER, "failed", "", [], [], [], "", request_id, why, trace_id=trace_id)
            r = TurnResult(out["answer"], out["status"], out["scope"], out["visible"],
                           [t["tool"] for t in out["tool_log"]], out["sources"], out["plan"]["intent"],
                           request_id, out["error"],
                           extra={"tool_calls": [t for t in out["tool_log"] if not t.get("rejected")],
                                  "evidence": out["evidence"], "handoff": out.get("handoff"), "checklist": out.get("checklist"),
                                  "handoff_calls": out.get("handoff_calls") or 0, "decision": out.get("decision"),
                                  "paused": bool(out.get("__interrupt__")), "pending": out.get("pending")},
                           trace_id=trace_id)
            lf.update_current_span(
                output=r.answer, level="ERROR" if r.status == "failed" else None,
                metadata={**identity, "scope": r.scope, "intent": r.intent, "visible_tools": r.visible_tools,
                          "tool_sequence": r.tool_sequence, "write_tools_visible": [t for t in r.visible_tools if t in WRITE_TOOLS],
                          "status": r.status, "sources": r.sources, "paused": r.extra["paused"],
                          "fallbacks": out.get("fallbacks") or [], "trajectory": digest(out)})
            return r

        if toolbox is not None:
            return await run(toolbox)
        async with mcp_client.open_toolbox(caller_id) as box:
            return await run(box)


def handle_turn(*a, **kw) -> TurnResult:
    return asyncio.run(ahandle_turn(*a, **kw))


async def aresume_pending(caller_id: str, caller_group: str, thread_id: str, as_of: str,
                          toolbox=None, checkpoint_path=None, environment: str | None = None) -> TurnResult:
    """Resume a paused approval, possibly in a new process. The recorded approval rows decide: while any is not
    'approved' nothing is released and the checkpoint stays paused. Nothing in a resume message is ever read.
    Traced as its own root, `agent.resume`, carrying the ORIGINAL request id so the pause and the resume read as one story."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.types import Command
    path = checkpoint_path or CHECKPOINT_DB

    @observe(name="agent.resume", as_type="agent", capture_input=False, capture_output=False)
    async def go(box):
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            g = graph.build_graph(saver)
            st = await g.aget_state({"configurable": {"thread_id": thread_id}})
            pend = {k: v for k, v in (st.values.get("pending") or {}).items() if not v["released"]}
            origin = next((p.get("origin_request_id") for p in pend.values() if p.get("origin_request_id")), "")
            rid = origin or f"{thread_id}:resume"
            lf = get_client()
            box.ctx = {"as_of": as_of, "conversation_id": thread_id, "request_id": rid, "trace_id": lf.get_current_trace_id() or ""}
            identity = {"caller_id": caller_id, "caller_group": caller_group, "request_id": rid, "as_of": as_of,
                        "case_id": thread_id, "turn": "resume", "resume_of": origin,
                        "origin_trace_id": next((p.get("origin_trace_id", "") for p in pend.values()), "")}
            lf.update_current_span(input="resume pending approvals", metadata=identity)
            cfg = {"configurable": {"thread_id": thread_id, "toolbox": box, "as_of": as_of, "request_id": rid}}
            with propagate_attributes(session_id=thread_id, user_id=caller_id, metadata=identity, environment=environment,
                                      tags=["novaops", "resume"]):
                r = await _resume(box, g, cfg, st, pend, Command)
            r.trace_id = box.ctx["trace_id"]
            lf.update_current_span(output=r.answer, metadata={**identity, "status": r.status, "scope": "webex",
                                                              "paused": r.status == "pending", "pending": list(pend)})
            return r

    if toolbox is not None:
        return await go(toolbox)
    async with mcp_client.open_toolbox(caller_id) as box:
        return await go(box)


async def _resume(box, g, cfg, st, pend, Command) -> TurnResult:
    rid = cfg["configurable"]["request_id"]
    if not pend:
        return TurnResult("Nothing is pending on this conversation.", "completed", "webex", [], [], [], "access_request", rid)
    checks = {k: await release_pending_check(box, p) for k, p in pend.items()}
    if not all(checks.values()):  # rows not all approved: stay paused, write nothing
        text = " ".join([graph.resume_text(k, {"released": False, "approvals": await _status(box, p)}) for k, p in pend.items()])
        return TurnResult(text, "pending", "webex", [], [], [], "access_request", rid)
    if st.next:  # the interrupt is still there: resume the graph itself
        out = await g.ainvoke(Command(resume="rows-recorded"), cfg)
        return TurnResult(out["answer"], out["status"], "webex", [], [], [], "access_request", rid, extra={"pending": out.get("pending")})
    # the interrupt was dropped by a later turn on this thread: same release, from the persisted pending record
    notes, new = [], dict(st.values["pending"])
    for k, p in pend.items():
        r = await release_pending(box, p)
        new[k] = {**p, "released": r["released"]}
        notes.append(graph.resume_text(k, r))
    await g.aupdate_state(cfg, {"pending": new})
    return TurnResult(" ".join(notes), "needs_human", "webex", [], [], [], "access_request", rid, extra={"pending": new})


async def _status(box, p: dict) -> dict:
    return {a["approval_id"]: a["status"] for a in await box.call("read_approval", {"request_id": p["request_id"]})}


async def release_pending_check(box, p: dict) -> bool:
    s = await _status(box, p)
    return bool(s) and all(v == "approved" for v in s.values())


def resume_pending(*a, **kw) -> TurnResult:
    return asyncio.run(aresume_pending(*a, **kw))
