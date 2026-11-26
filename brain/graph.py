"""classify -> scope -> gather -> answer, on LangGraph with a SQLite checkpointer.
Adapted from lesson-10 02-dynamic-tool-loadout/graph.py. The graph reaches tools only through the
Toolbox (MCP); it never touches the database. Model calls only through llm.complete."""
import json
import operator
import re
from typing import Annotated, TypedDict

from langfuse import get_client, observe
from langgraph.types import interrupt

from brain import checklist, gate, llm
from brain.handoff import WebexHandoff, make_key
from brain.plan import CLASSIFY_SYSTEM, PLAN_SCHEMA, TurnPlan, classify_prompt, parse_plan, render, window
from brain.scope import READ_TOOLS, WRITE_TOOLS, select_tools, widen_reads
from brain.status import answer_agrees, final_status
from brain.webex import AccessDecision, release_pending

MAX_STEPS = 6
MAX_FACTS = 60


class State(TypedDict, total=False):
    messages: Annotated[list, operator.add]   # whole conversation, survives restarts
    facts: list
    constraints: list
    handoffs: dict        # idempotency key -> handoff already made in this conversation
    handoff: dict         # this turn's handoff, if any
    handoff_calls: int
    pending: dict         # request id -> approvals dispatched and not yet released (the durable pause)
    pause_now: bool       # this turn dispatched approvals: pause after the answer
    decision: dict        # this turn's typed AccessDecision, built by code
    checklist: dict
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
    return {"plan": plan.model_dump(), "closed": closed, "handoff": None, "decision": None, "pause_now": False, "checklist": None, "facts": _union(facts, plan.relevant_facts),
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
    needed, nudged = TurnPlan(**state["plan"]).lookups_needed, False
    for _ in range(MAX_STEPS):
        user = (f"Caller employee id (use it ONLY for 'me'/'my'; any other person needs their own lookup): {box.caller_id}\n"
                f"Constraints: {state.get('constraints')}\nFacts: {state.get('facts')}\n"
                f"Recent conversation:\n{render(_window(state))}\n\n"
                f"Newest message:\n{state['message']}\n\nLookups this question needs: {needed or '(not listed)'}\n\nEvidence so far:\n{_evidence_text(ev)}")
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
            made = sum(1 for t in log if not t.get("rejected"))
            if needed and made < min(len(needed), 3) and not nudged:  # one bounded refinement, never a search-until-found loop
                nudged = True
                ev.append({"tool": "(planner)", "args": {}, "result": f"NOT DONE: the question still needs {needed}"})
                continue
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
    plan = TurnPlan(**state["plan"])
    if (plan.intent == "access_request" and plan.system_name.strip() and "search_documents" in visible and not error
            and not any("it_kb" in x for x in srcs)):  # #13: the login article is not reliably ranked; one targeted search
        args = {"query": f"{plan.system_name} license login account not licensed", "corpus": "it_kb"}
        try:
            res = await box.call("search_documents", args)
            log.append({"tool": "search_documents", "args": args})
            ev.append({"tool": "search_documents", "args": args, "result": res})
            srcs += _sources(res)
        except Exception as e:  # noqa: BLE001
            error = f"search_documents failed: {e}"
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
    caller = cfg["toolbox"].caller_id
    # consent: an explicit go-ahead in the caller's own current words, or the caller asking for themselves while
    # no "do not file" rule is in force (that escalates to humans and grants nothing). Both are regex heuristics.
    me = _employees(state).get(caller, "").lower()
    subj = plan.subject_name.strip()
    # the planner's subject_name is free text ("user", "caller", "new hire in Customer Success"): it names ANOTHER
    # person only if it reads like a name (capitalised) and is not the caller's own. A heuristic, tested.
    about_me = not subj or subj.lower() in ("user", "me", "myself", "i", "caller", "requester", me, caller.lower()) or not subj[:1].isupper()
    mine = (about_me and gate.self_request(state["message"]) and not gate.blocks_filing(state.get("constraints")))
    if plan.intent != "access_request" or not (gate.consent_from_message(state["message"]) or mine):
        return {"handoff": None}
    seen = _employees(state)
    if not (mine and not gate.consent_from_message(state["message"])) and subj and not any(n.lower() == subj.lower() for n in seen.values()):
        # the caller named someone no earlier lookup returned: code resolves them (exact full name), never the model
        for r in await cfg["toolbox"].call("get_employee", {"query": subj}):
            if isinstance(r, dict) and r.get("employee_id"):
                seen[r["employee_id"]] = r["full_name"]
    ids = [caller] if mine and not gate.consent_from_message(state["message"]) else [
        i for i, n in seen.items() if plan.subject_name and n.lower() == plan.subject_name.lower()]
    if len(ids) != 1 or not plan.system_name.strip():
        _meta(handoff="not made", matches=ids, system=plan.system_name)
        return {"handoff": None}
    h = WebexHandoff(subject_employee_id=ids[0], system=plan.system_name.strip(),
                     justification=f"Requested by {cfg['toolbox'].caller_id}: {gate.top_level(state['message']).strip()[:200]}",
                     conversation_id=cfg["thread_id"], caller_id=cfg["toolbox"].caller_id,
                     idempotency_key=make_key(ids[0], plan.system_name, cfg["thread_id"]))
    made, calls = dict(state.get("handoffs") or {}), state.get("handoff_calls") or 0
    pend, pause_now = dict(state.get("pending") or {}), False
    if h.idempotency_key not in made:  # a replay never reaches the port a second time
        with get_client().start_as_current_observation(name="approval_transition", as_type="span",
                                                        input={"handoff": h.model_dump()}) as sp:
            res = await cfg["webex_port"].request_access(h)
            sp.update(output=res.decision, metadata={"request_id": cfg["request_id"], "pending": res.pending})
        made[h.idempotency_key] = {**h.model_dump(), "result": res.model_dump()}
        calls += 1
        if res.pending:
            pend[res.pending["request_id"]] = res.pending
            pause_now = True
    _meta(handoff=made[h.idempotency_key], handoff_calls=calls)
    dec = made[h.idempotency_key]["result"].get("decision")
    ev, srcs = list(state.get("evidence") or []), list(state.get("sources") or [])
    if dec and dec.get("observed_facts"):  # what the workflow read is evidence too, so every citation is traceable
        rows = [{"source": f["source"], "text": f["text"]} for f in dec["observed_facts"]]
        ev.append({"tool": "webex_workflow", "args": {"subject": h.subject_employee_id, "system": h.system}, "result": rows})
        srcs = sorted(set(srcs) | {r["source"] for r in rows})
    return {"handoff": made[h.idempotency_key], "handoffs": made, "handoff_calls": calls, "pending": pend,
            "pause_now": pause_now, "decision": dec, "evidence": ev, "sources": srcs}


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
    dec = state.get("decision")
    if dec:  # the typed AccessDecision is built by code; its rendering is appended verbatim to the model's prose
        d = AccessDecision(**dec)
        status = d.status
        text += " " + d.render()
    _meta(status=status)
    # The assistant's own sentences are never stored as facts: facts come from the user's words only.
    cl = checklist.build(state["evidence"])
    return {"answer": text, "status": status, "checklist": cl.model_dump() if cl else None, "facts": _union(state.get("facts"), [f"{plan.intent}: {f}" for f in _row_facts(state["evidence"])]),
            "messages": [{"role": "user", "content": state["message"], "intent": plan.intent},
                         {"role": "assistant", "content": text, "intent": plan.intent}]}


@observe(name="pause")
async def pause(state: State, config) -> dict:
    """The durable pause (lesson 9). Approvals were dispatched this turn: persist and stop here. Whatever value
    resumes this node is ignored: only the approval rows, re-read through MCP, decide whether anything is released."""
    if not state.get("pause_now"):
        return {}
    interrupt({"pending": list(state["pending"]), "message": "waiting for recorded approvals"})
    box = config["configurable"]["toolbox"]
    pend, notes = dict(state["pending"]), []
    for rid, p in pend.items():
        if p["released"]:
            continue
        r = await release_pending(box, p)
        if r["released"]:
            pend[rid] = {**p, "released": True}
        notes.append(resume_text(rid, r))
    text = " ".join(notes)
    _meta(resumed=True, released=[k for k, v in pend.items() if v["released"]])
    return {"pending": pend, "answer": text, "status": "needs_human" if all(v["released"] for v in pend.values()) else "pending",
            "messages": [{"role": "assistant", "content": text}]}


def resume_text(rid: str, r: dict) -> str:
    if r["released"]:
        return (f"The recorded approvals ({', '.join(sorted(r['approvals']))}) are approved, so request {rid} is released to IT. "
                "No seat has been assigned: IT still has to assign one once capacity exists.")
    waiting = ", ".join(k for k, v in r["approvals"].items() if v != "approved")
    return f"Request {rid} is still waiting on {waiting}. Nothing has been released."


def route_classify(state: State) -> str:
    return "answer" if state["plan"]["intent"] == "ambiguous" else "scope"


def after_scope(state: State) -> str:
    return "gather" if state["visible"] else ("handoff" if state["plan"]["intent"] == "access_request" else "answer")


def build_graph(checkpointer):
    from langgraph.graph import END, START, StateGraph
    g = StateGraph(State)
    for n, f in [("classify", classify), ("scope", scope), ("gather", gather), ("rearm", rearm), ("handoff", handoff),
                 ("answer", answer), ("pause", pause)]:
        g.add_node(n, f)
    g.add_edge(START, "classify")
    g.add_conditional_edges("classify", route_classify, {"scope": "scope", "answer": "answer"})
    g.add_conditional_edges("scope", after_scope, {"gather": "gather", "handoff": "handoff", "answer": "answer"})
    g.add_conditional_edges("gather", after_gather, {"rearm": "rearm", "handoff": "handoff", "answer": "answer"})
    g.add_edge("rearm", "gather")
    g.add_edge("handoff", "answer")
    g.add_edge("answer", "pause")
    g.add_edge("pause", END)
    return g.compile(checkpointer=checkpointer)
