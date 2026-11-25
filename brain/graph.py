"""classify -> scope -> gather -> answer, on LangGraph with a SQLite checkpointer.
Adapted from lesson-10 02-dynamic-tool-loadout/graph.py. The graph reaches tools only through the
Toolbox (MCP); it never touches the database. Model calls only through llm.complete."""
import json
import operator
import re
from typing import Annotated, TypedDict

from langfuse import get_client, observe

from brain import gate, llm
from brain.handoff import WebexHandoff, make_key
from brain.plan import CLASSIFY_SYSTEM, PLAN_SCHEMA, TurnPlan, classify_prompt, parse_plan, render, window
from brain.scope import READ_TOOLS, WRITE_TOOLS, select_tools, widen_reads
from brain.status import answer_agrees, final_status

MAX_STEPS = 6
MAX_FACTS = 60


class State(TypedDict, total=False):
    messages: Annotated[list, operator.add]   # whole conversation, survives restarts
    facts: list
    constraints: list
    handoffs: dict        # idempotency key -> handoff already made in this conversation
    handoff: dict         # this turn's handoff, if any
    handoff_calls: int
    closed: list          # intents of topics the user closed (their raw turns and rows are dropped)
    # per turn
    message: str
    plan: dict
    scope: str
    visible: list
    evidence: list
    tool_log: list
    sources: list
    rearmed: bool
    error: str
    status: str
    answer: str


def _union(old, new):
    return list(dict.fromkeys([*(old or []), *(new or [])]))[-MAX_FACTS:]  # ponytail: facts/constraints only grow


def _window(state):
    return window(state.get("messages") or [], closed=state.get("closed") or ())


def _meta(**kw):
    get_client().update_current_span(metadata=kw)


@observe(name="classify")
async def classify(state: State, config) -> dict:
    msgs = classify_prompt(_window(state), state["message"], state.get("facts"), state.get("constraints"))
    try:
        plan = parse_plan(llm.structured(msgs, CLASSIFY_SYSTEM, PLAN_SCHEMA, name="classify", max_tokens=500))
    except ValueError:  # no valid plan twice: fail open for reads only
        plan = TurnPlan()
    _meta(intent=plan.intent, requires_tools=plan.requires_tools, refuse=plan.refuse)
    facts, closed = list(state.get("facts") or []), [c for c in state.get("closed") or [] if c != plan.intent]
    if plan.closes_topic:  # the model proposes, code applies: drop the last topic's rows, keep one decision line
        prev = next((m.get("intent") for m in reversed(state.get("messages") or []) if m["role"] == "user"), None)
        if prev and prev != plan.intent:
            gone = [f for f in facts if f.startswith(f"{prev}: ")]
            ids = set(re.findall(r"\[db:\w+/(\w+)\]", " ".join(gone)))  # the dropped rows' own ids only
            facts = [f for f in facts if f not in gone and not (ids and not f.startswith("decision:")
                                                                   and any(i in f for i in ids))]
            closed.append(prev)
        facts.append(f"decision: closed - {plan.closes_topic}")
    return {"plan": plan.model_dump(), "closed": closed, "handoff": None, "facts": _union(facts, plan.relevant_facts),
            "constraints": _union(state.get("constraints"), plan.relevant_constraints),
            "evidence": [], "tool_log": [], "sources": [], "rearmed": False, "error": "", "answer": "", "scope": "none", "visible": []}


@observe(name="scope")
async def scope(state: State, config) -> dict:
    scope_name, visible = select_tools(TurnPlan(**state["plan"]), gate_open=False)
    _meta(scope=scope_name, visible_tools=visible, write_tools_visible=[t for t in visible if t in WRITE_TOOLS])
    return {"scope": scope_name, "visible": visible}


GATHER_SYSTEM = (
    "You gather evidence for a NovaOps assistant. Reply with ONLY one JSON object:\n"
    '  {{"tool": "<name>", "args": {{...}}}}  to call a visible tool, or  {{"done": true}}  when you have enough.\n'
    "Visible tools this turn (no others exist for you):\n{tools}\n"
    "Rules: do not repeat a call that already has a result; use employee ids from results, never guess them; "
    "'she/he/her/him/they' means the person named in the conversation, NEVER the caller: look that person up by full name and use their id; "
    "looking up who someone is does NOT answer a question about their tickets, tasks or assets: call that records tool with the id before done; "
    "stop with done as soon as the question can be answered. Never call anything to file or approve.")


def _evidence_text(ev: list) -> str:
    return "\n".join(f"[{i}] {e['tool']}({json.dumps(e['args'])}) -> {json.dumps(e['result'])[:1500]}"
                     for i, e in enumerate(ev, 1)) or "(none yet)"


def _sources(result) -> list[str]:
    out = []
    for part in result if isinstance(result, list) else [result]:
        if isinstance(part, dict) and part.get("source"):
            out.append(part["source"])
    return out


@observe(name="gather")
async def gather(state: State, config) -> dict:
    box = config["configurable"]["toolbox"]
    visible, ev, log, srcs = state["visible"], list(state["evidence"]), list(state["tool_log"]), list(state["sources"])
    error = ""
    bad = 0
    for _ in range(MAX_STEPS):
        user = (f"Caller employee id (use it ONLY for 'me'/'my'; any other person needs their own lookup): {box.caller_id}\n"
                f"Constraints: {state.get('constraints')}\nFacts: {state.get('facts')}\n"
                f"Recent conversation:\n{render(_window(state))}\n\n"
                f"Newest message:\n{state['message']}\n\nEvidence so far:\n{_evidence_text(ev)}")
        schema = {"type": "object", "properties": {"tool": {"type": "string", "enum": list(visible)},
                                                   "args": {"type": "object"}, "done": {"type": "boolean"}}}
        try:
            act = llm.structured([{"role": "user", "content": user}], GATHER_SYSTEM.format(tools=box.describe(visible)),
                                 schema, name="action", max_tokens=400)
        except ValueError:
            bad += 1
            if bad > 1:
                error = "model did not return a valid action"
                break
            continue
        if act.get("done") or not act.get("tool"):
            break
        name, args = act["tool"], act.get("args") or {}
        if name not in visible or name not in box.specs:  # defence in depth: the executor enforces scope
            log.append({"tool": name, "rejected": True})
            ev.append({"tool": name, "args": args, "result": "REJECTED: tool not visible this turn"})
            continue
        gap = box.missing_args(name, args)
        if gap:  # tell the model once, do not crash the turn on a bad call
            log.append({"tool": name, "rejected": True})
            ev.append({"tool": name, "args": args, "result": f"REJECTED: missing required argument(s) {gap}"})
            continue
        if any(e["tool"] == name and e["args"] == args for e in ev):  # a repeat call means the model is looping
            break
        with get_client().start_as_current_observation(name=f"tool.{name}", as_type="tool",
                                                        input={"args": args}) as sp:
            try:
                res = await box.call(name, args)
            except Exception as e:  # noqa: BLE001
                error = f"{name} failed: {e}"
                sp.update(level="ERROR", status_message=error)
                break
            found = _sources(res)
            sp.update(output={"sources": found, "n": len(res)}, metadata={"request_id": config["configurable"]["request_id"]})
        log.append({"tool": name, "args": args})
        ev.append({"tool": name, "args": args, "result": res})
        srcs += found
    _meta(tool_sequence=[t["tool"] for t in log], rejected=[t["tool"] for t in log if t.get("rejected")],
          sources=sorted(set(srcs)), error=error)
    return {"evidence": ev, "tool_log": log, "sources": sorted(set(srcs)), "error": error}


def after_gather(state: State) -> str:
    plan = TurnPlan(**state["plan"])
    used = any(not t.get("rejected") for t in state["tool_log"])
    if plan.requires_tools and not used and not state["rearmed"] and not state["error"]:
        return "rearm"
    return "handoff" if plan.intent == "access_request" and not state["error"] else "answer"


@observe(name="rearm")
async def rearm(state: State, config) -> dict:
    """The plan said a lookup was needed and none ran: widen to every READ tool, once. Never adds a write."""
    wide = widen_reads(state["visible"])
    _meta(visible_tools=wide)
    return {"visible": wide, "rearmed": True}


def _employees(state: State) -> dict:
    """id -> full name for every employee row this conversation has seen (facts and this turn's evidence)."""
    seen = {}
    for f in state.get("facts") or []:
        m = re.search(r"\[db:employees/(E\d+)\].*?full_name=([^,]+)", f)
        if m:
            seen[m.group(1)] = m.group(2).strip()
    for e in state.get("evidence") or []:
        for r in e["result"] if isinstance(e["result"], list) else []:
            if isinstance(r, dict) and r.get("employee_id") and r.get("full_name"):
                seen[r["employee_id"]] = r["full_name"]
    return seen


@observe(name="handoff")
async def handoff(state: State, config) -> dict:
    """Maya proposes, never files. On an explicit ask to act, code builds ONE typed handoff for the Webex side.
    The subject is matched by exact full name against employees already seen, so Maya Cohen is never Maya Rosen."""
    plan = TurnPlan(**state["plan"])
    cfg = config["configurable"]
    if plan.intent != "access_request" or not gate.consent_from_message(state["message"]):
        return {"handoff": None}
    ids = [i for i, n in _employees(state).items() if plan.subject_name and n.lower() == plan.subject_name.lower()]
    if len(ids) != 1 or not plan.system_name.strip():
        _meta(handoff="not made", matches=ids, system=plan.system_name)
        return {"handoff": None}
    h = WebexHandoff(subject_employee_id=ids[0], system=plan.system_name.strip(),
                     justification=f"Requested by {cfg['toolbox'].caller_id}: {gate.top_level(state['message']).strip()[:200]}",
                     conversation_id=cfg["thread_id"], caller_id=cfg["toolbox"].caller_id,
                     idempotency_key=make_key(ids[0], plan.system_name, cfg["thread_id"]))
    made, calls = dict(state.get("handoffs") or {}), state.get("handoff_calls") or 0
    if h.idempotency_key not in made:  # a replay never reaches the port a second time
        res = await cfg["webex_port"].request_access(h)
        made[h.idempotency_key] = {**h.model_dump(), "result": res.model_dump()}
        calls += 1
    _meta(handoff=made[h.idempotency_key], handoff_calls=calls)
    return {"handoff": made[h.idempotency_key], "handoffs": made, "handoff_calls": calls}


ANSWER_SYSTEM = (
    "You are the NovaOps internal assistant. Answer the user's CURRENT request from the evidence and the "
    "established facts only; never invent an employee, number or rule. Cite every claim by the exact `source` string of the evidence row, in brackets, for example "
    "[documents/policies/byod_policy.md] or [db:onboarding_tasks/OT005]. Never use numbered citations like [1], and never cite a source that is not in the evidence. First state the observed facts from the evidence (seat counts, ticket ids and status, names); only then what cannot be done. Always state employee ids (E0xx) and exact numbers you relied on. Constraints the user stated remain in force. Newer or more specific guidance overrides older. "
    "You cannot file, create, approve or grant anything in this conversation: never say you did, and for a "
    "request to file something, say it has NOT been filed and what would be needed. If you could not look "
    "something up, say so. At most six sentences. Terminal status of this turn: {status}.")


def _row_facts(evidence: list) -> list[str]:
    """Memory of what the tools returned, written by code: one compact line per database row. Never the
    model's own prose, so a wrong sentence cannot become a fact. (Top document passages are kept too, so a follow-up needs no new search.)"""
    out = []
    for e in evidence:
        docs = 0
        for part in e["result"] if isinstance(e["result"], list) else []:
            if not isinstance(part, dict):
                continue
            src = str(part.get("source", ""))
            if src.startswith("db:"):
                body = ", ".join(f"{k}={v}" for k, v in part.items() if k != "source" and v not in (None, ""))
                out.append(f"[{src}] {body}"[:240])
            elif src.startswith("documents/") and part.get("text") and docs < 3:  # top passages, so follow-ups need no new search
                docs += 1
                out.append(f"[{src}] {part.get('section', '')}: {' '.join(part['text'].split())[:300]}")
    return out


@observe(name="answer")
async def answer(state: State, config) -> dict:
    plan = TurnPlan(**state["plan"])
    status = final_status(error=bool(state["error"]), clarify=plan.intent == "ambiguous", refuse=plan.refuse,
                          action_unreleased=plan.intent == "access_request")
    if plan.intent == "ambiguous":
        text = plan.clarify_question or "Which one do you mean? Please give a name or system."
    elif status == "failed":
        text = f"I could not complete this: {state['error']}."
    else:
        body = (f"Facts: {state.get('facts')}\nConstraints: {state.get('constraints')}\n"
                f"Recent conversation:\n{render(_window(state))}\n\n"
                f"Evidence:\n{_evidence_text(state['evidence'])}\n\nQuestion:\n{state['message']}")
        if state.get("handoff"):
            body += (f"\n\nA handoff to the Webex workflow was recorded as PENDING for {state['handoff']['subject_employee_id']} "
                     f"({state['handoff']['system']}). It is not filed, approved or granted.")
        text = llm.complete([{"role": "user", "content": body}], ANSWER_SYSTEM.format(status=status), max_tokens=700)
    h = state.get("handoff")
    if not answer_agrees(status, text):
        _meta(guard_rewrote_answer=True, original_answer=text[:400])
        text = ("I have not filed or granted anything. That would need the caller's explicit go-ahead "
                "and the recorded approvals first.")
    if h and "pending" not in text.lower():  # the application, not the model, guarantees the honest outcome
        text += f" The {h['system']} access request for {h['subject_employee_id']} is pending; it is not filed, approved or in effect yet."
    _meta(status=status)
    # The assistant's own sentences are never stored as facts: facts come from the user's words only.
    return {"answer": text, "status": status, "facts": _union(state.get("facts"), [f"{plan.intent}: {f}" for f in _row_facts(state["evidence"])]),
            "messages": [{"role": "user", "content": state["message"], "intent": plan.intent},
                         {"role": "assistant", "content": text, "intent": plan.intent}]}


def route_classify(state: State) -> str:
    return "answer" if state["plan"]["intent"] == "ambiguous" else "scope"


def after_scope(state: State) -> str:
    return "gather" if state["visible"] else ("handoff" if state["plan"]["intent"] == "access_request" else "answer")


def build_graph(checkpointer):
    from langgraph.graph import END, START, StateGraph
    g = StateGraph(State)
    for n, f in [("classify", classify), ("scope", scope), ("gather", gather), ("rearm", rearm), ("handoff", handoff),
                 ("answer", answer)]:
        g.add_node(n, f)
    g.add_edge(START, "classify")
    g.add_conditional_edges("classify", route_classify, {"scope": "scope", "answer": "answer"})
    g.add_conditional_edges("scope", after_scope, {"gather": "gather", "handoff": "handoff", "answer": "answer"})
    g.add_conditional_edges("gather", after_gather, {"rearm": "rearm", "handoff": "handoff", "answer": "answer"})
    g.add_edge("rearm", "gather")
    g.add_edge("handoff", "answer")
    g.add_edge("answer", END)
    return g.compile(checkpointer=checkpointer)
