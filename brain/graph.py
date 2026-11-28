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
from brain.status import answer_agrees, claims_done, final_status
from brain.webex import AccessDecision, release_pending

MAX_STEPS = 6
MAX_FACTS = 60
_GENERIC_SUBJECT = re.compile(r"\b(user|caller|requester|requestor|myself|me|i|employee|hire|joiner|person|someone)\b", re.I)


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
    fallbacks: list       # silent degradations that fired this turn (classify default plan, gather gave up)


def _union(old, new, cap: int | None = MAX_FACTS):
    out = list(dict.fromkeys([*(old or []), *(new or [])]))
    return out[-cap:] if cap else out  # facts keep the newest `cap`; constraints use cap=None: a user's rule is never dropped


def _window(state):
    return window(state.get("messages") or [], closed=state.get("closed") or ())


def _meta(**kw):
    get_client().update_current_span(metadata=kw)


def _io(inp, out, **kw):
    """Compact input/output for a node span: what went in and what came out, not the whole state."""
    get_client().update_current_span(input=inp, output=out, metadata=kw or None)


def _node(name):
    return observe(name=name, capture_input=False, capture_output=False)


@_node("classify")
async def classify(state: State, config) -> dict:
    msgs = classify_prompt(_window(state), state["message"], state.get("facts"), state.get("constraints"))
    fallbacks = []
    try:
        plan = parse_plan(llm.structured(msgs, CLASSIFY_SYSTEM, PLAN_SCHEMA, name="classify", max_tokens=1000))
    except ValueError as e:  # no valid plan twice: fail open for reads only, and SAY SO in the trace
        plan = TurnPlan()
        fallbacks.append("classify_default_plan")
        get_client().update_current_span(level="WARNING", status_message=f"classify fell back to the default plan: {e}"[:300])
    if (not plan.requires_tools and not plan.refuse and plan.intent != "ambiguous"
            and not state.get("messages") and not state.get("facts")):
        plan.requires_tools = True  # the prompt says a first question needs a lookup; code enforces it: nothing to answer from
        fallbacks.append("requires_tools_forced_first_turn")
    _io({"message": state["message"][:300], "constraints": state.get("constraints")},
        {"intent": plan.intent, "requires_tools": plan.requires_tools, "lookups_needed": plan.lookups_needed,
         "system": plan.system_name, "subject": plan.subject_name, "closes_topic": plan.closes_topic, "refuse": plan.refuse},
        classify_fallback=bool(fallbacks))
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
            "constraints": _union(state.get("constraints"), plan.relevant_constraints, cap=None),
            "evidence": [], "tool_log": [], "sources": [], "rearmed": False, "error": "", "answer": "", "scope": "none", "visible": [],
            "fallbacks": fallbacks}


@_node("scope")
async def scope(state: State, config) -> dict:
    scope_name, visible = select_tools(TurnPlan(**state["plan"]), gate_open=False)
    _io({"intent": state["plan"]["intent"], "requires_tools": state["plan"].get("requires_tools")},
        {"scope": scope_name, "visible_tools": visible}, write_tools_visible=[t for t in visible if t in WRITE_TOOLS])
    return {"scope": scope_name, "visible": visible}


GATHER_SYSTEM = (
    "You gather evidence for a NovaOps assistant. Reply with ONLY one JSON object:\n"
    '  {{"tool": "<name>", "args": {{...}}}}  to call a visible tool, or  {{"done": true}}  when you have enough.\n'
    "Visible tools this turn (no others exist for you):\n{tools}\n"
    "Rules: do not repeat a call that already has a result; use employee ids from results, never guess them; "
    "'she/he/her/him/they' means the person named in the conversation, NEVER the caller: look that person up by full name and use their id; "
    "looking up who someone is does NOT answer a question about their tickets, tasks or assets: call that records tool with the id before done; "
    "stop with done as soon as the question can be answered. Never call anything to file or approve.")


EVIDENCE_CHARS = 12000  # the whole evidence block. 8 whole-document results are about 1400 characters each
MIN_ITEM_CHARS = 1500   # no single result is squeezed below this, however many there are
_DOC_WORDS = re.compile(r"polic|rule|threshold|handbook|document|memo|article|eligib|guideline|procedure|contract term|"
                        r"letter|addendum|agreement|summary|statement of work|profile|\boffer\b|\bform\b", re.I)
_PRONOUN = re.compile(r"\b(her|his|she|he|their|they|them)\b", re.I)


def _search_query(state) -> str:
    """The user's own words for a forced search. A pronoun ('her offer letter') names nobody to a search engine, so the
    person most recently seen in this conversation (a database row, never the model's prose) is put in front."""
    q = gate.top_level(state["message"]).strip()[:300]
    names = list(_employees(state).values())
    return f"{names[-1]} {q}" if names and _PRONOUN.search(q) else q


def _shrink(result, limit: int = EVIDENCE_CHARS):
    """-> (result, chars_cut). Cap a tool result near `limit` characters by shortening each row's `text`, never by
    cutting the JSON string (a cut string once ended mid-`source` and the model cited '[documents/handbook/getting-.]').
    A shortened row ends with a marker, so the model knows the text is partial."""
    if isinstance(result, str):
        return (result, 0) if len(result) <= limit else (result[:limit] + " ...[truncated]", len(result) - limit)
    if not isinstance(result, list) or len(json.dumps(result)) <= limit:
        return result, 0
    rows = [r for r in result if isinstance(r, dict) and isinstance(r.get("text"), str)]
    if not rows:
        return result, 0
    room = limit - (len(json.dumps(result)) - sum(len(r["text"]) for r in rows))
    cap = max(120, room // len(rows))
    cut, out = 0, []
    for r in result:
        if isinstance(r, dict) and isinstance(r.get("text"), str) and len(r["text"]) > cap:
            cut += len(r["text"]) - cap
            r = {**r, "text": r["text"][:cap] + f" ...[truncated, {len(r['text']) - cap} more characters]"}
        out.append(r)
    return out, cut


def _evidence(ev: list) -> tuple[str, int]:
    """-> (prompt text, characters cut). One budget for the whole block, shared between the results."""
    each = max(MIN_ITEM_CHARS, EVIDENCE_CHARS // max(1, len(ev)))
    lines, cut = [], 0
    for i, e in enumerate(ev, 1):
        res, c = _shrink(e["result"], each)
        cut += c
        lines.append(f"[{i}] {e['tool']}({json.dumps(e['args'])}) -> {json.dumps(res)}")
    return "\n".join(lines) or "(none yet)", cut


def _evidence_text(ev: list) -> str:
    return _evidence(ev)[0]


def _sources(result) -> list[str]:
    out = []
    for part in result if isinstance(result, list) else [result]:
        if isinstance(part, dict) and part.get("source"):
            out.append(part["source"])
    return out


@_node("gather")
async def gather(state: State, config) -> dict:
    box = config["configurable"]["toolbox"]
    visible, ev, log, srcs = state["visible"], list(state["evidence"]), list(state["tool_log"]), list(state["sources"])
    error = ""
    bad = failures = 0
    fallbacks = list(state.get("fallbacks") or [])
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
                fallbacks.append("gather_gave_up")
                break
            continue
        if act.get("done") or not act.get("tool"):
            made = sum(1 for t in log if not t.get("rejected") and not t.get("failed"))
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
        try:
            res = await box.call(name, args)  # the tool.<name> span is opened inside Toolbox.call
        except Exception as e:  # noqa: BLE001  one failed tool must not throw away what was already gathered
            failures += 1
            fallbacks.append(f"tool_failed:{name}")
            log.append({"tool": name, "args": args, "failed": True})
            ev.append({"tool": name, "args": args, "result": f"ERROR: {name} failed ({str(e)[:200]}). Say this could not be looked up."})
            if failures >= 2:  # a second failure: give up, the turn is failed
                error = f"{name} failed: {e}"
                break
            continue
        found = _sources(res)
        log.append({"tool": name, "args": args})
        ev.append({"tool": name, "args": args, "result": res})
        srcs += found
    plan = TurnPlan(**state["plan"])
    if (plan.requires_tools and "search_documents" in visible and not error
            and not any(t["tool"] == "search_documents" and not t.get("rejected") and not t.get("failed") for t in log)
            and (plan.intent == "policy_question" or any(_DOC_WORDS.search(x) for x in needed)
                 or _DOC_WORDS.search(gate.top_level(state["message"])))):
        # code enforces what classify asked for: a question that needs a document is not answered without a search
        args = {"query": _search_query(state)}
        fallbacks.append("forced_document_search")
        try:
            res = await box.call("search_documents", args)
            log.append({"tool": "search_documents", "args": args})
            ev.append({"tool": "search_documents", "args": args, "result": res})
            srcs += _sources(res)
        except Exception as e:  # noqa: BLE001
            fallbacks.append("tool_failed:search_documents")
            log.append({"tool": "search_documents", "args": args, "failed": True})
            ev.append({"tool": "search_documents", "args": args, "result": f"ERROR: search failed ({str(e)[:200]}). Say the document could not be searched."})
    if (plan.intent == "access_request" and plan.system_name.strip() and "search_documents" in visible and not error
            and not any("it_kb" in x for x in srcs)):  # #13: the login article is not reliably ranked; one targeted search
        args = {"query": f"{plan.system_name} license login account not licensed", "corpus": "it_kb"}
        try:
            res = await box.call("search_documents", args)
            log.append({"tool": "search_documents", "args": args})
            ev.append({"tool": "search_documents", "args": args, "result": res})
            srcs += _sources(res)
        except Exception as e:  # noqa: BLE001  a missing helper search must not fail the whole turn
            fallbacks.append("tool_failed:search_documents(it_kb)")
            log.append({"tool": "search_documents", "args": args, "failed": True})
    _io({"visible_tools": visible, "lookups_needed": needed},
        {"tool_sequence": [t["tool"] for t in log], "rejected": [t["tool"] for t in log if t.get("rejected")],
         "sources": sorted(set(srcs)), "error": error}, nudged=nudged)
    if error:
        get_client().update_current_span(level="ERROR", status_message=error[:300])
    return {"evidence": ev, "tool_log": log, "sources": sorted(set(srcs)), "error": error, "fallbacks": fallbacks}


def after_gather(state: State) -> str:
    plan = TurnPlan(**state["plan"])
    used = any(not t.get("rejected") and not t.get("failed") for t in state["tool_log"])
    if plan.requires_tools and not used and not state["rearmed"] and not state["error"]:
        return "rearm"
    return "handoff" if plan.intent == "access_request" and not state["error"] else "answer"


@_node("rearm")
async def rearm(state: State, config) -> dict:
    """The plan said a lookup was needed and none ran: widen to every READ tool, once. Never adds a write."""
    wide = widen_reads(state["visible"])
    _io({"visible_tools": state["visible"], "reason": "plan required a lookup and none ran"}, {"visible_tools": wide})
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


@_node("handoff")
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
    # the planner's subject_name is free text ("user", "caller", "new hire in Customer Success"). It names the caller
    # (or nobody) when it is empty, one of those generic words, or the caller's own name or id. Anything else is a
    # name: resolved case-insensitively against real employees, so "rachel stein" is Rachel, never the caller.
    about_me = (not subj or bool(_GENERIC_SUBJECT.search(subj)) or subj.lower() in (me, caller.lower()))
    mine = (about_me and gate.self_request(state["message"]) and not gate.blocks_filing(state.get("constraints")))
    explicit = gate.consent_from_message(state["message"])
    if plan.intent == "access_request":  # why a write may or may not follow, visible without reading the regexes
        with get_client().start_as_current_observation(name="consent_decision", as_type="guardrail",
                                                        input={"top_level_text": gate.top_level(state["message"])[:300]}) as sp:
            sp.update(output={"consent": bool(explicit or mine), "explicit_go_ahead": explicit, "self_request": mine,
                              "no_file_rule_in_force": gate.blocks_filing(state.get("constraints")), "about_caller": about_me,
                              "rule": "explicit go-ahead in own words" if explicit else ("own request, no do-not-file rule" if mine else "none")},
                      metadata={"request_id": cfg["request_id"], "method": "regex heuristic"})
    if plan.intent != "access_request" or not (explicit or mine):
        _io({"intent": plan.intent}, {"handoff": None}, reason="no consent for a handoff on this turn")
        return {"handoff": None}
    seen = _employees(state)
    if not about_me and not any(n.lower() == subj.lower() for n in seen.values()):
        # the caller named someone no earlier lookup returned: code resolves them (exact full name), never the model
        for r in await cfg["toolbox"].call("get_employee", {"query": subj}):
            if isinstance(r, dict) and r.get("employee_id"):
                seen[r["employee_id"]] = r["full_name"]
    # about the caller only when they say so themselves ("I need Webex ... go ahead and file it"); a bare "go ahead" names nobody
    ids = ([caller] if gate.self_request(state["message"]) else []) if about_me else [
        i for i, n in seen.items() if n.lower() == subj.lower()]
    if len(ids) != 1 or not plan.system_name.strip():
        _io({"subject": plan.subject_name, "system": plan.system_name}, {"handoff": None},
            reason="subject did not resolve to exactly one employee or no system", matches=ids)
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
            pend[res.pending["request_id"]] = {**res.pending, "origin_request_id": cfg["request_id"],
                                               "origin_trace_id": cfg["toolbox"].ctx.get("trace_id", "")}
            pause_now = True
    _io({"subject": h.subject_employee_id, "system": h.system, "key": h.idempotency_key},
        {"handoff_made": True, "replayed": h.idempotency_key in (state.get("handoffs") or {}), "pause": pause_now}, handoff_calls=calls)
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
    "something up, say so. A freeze or spending threshold means Finance approval above the threshold in the evidence; never say it blocks or defers purchases or provisioning unless the evidence says so. At most six sentences. Terminal status of this turn: {status}.")


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


@_node("answer")
async def answer(state: State, config) -> dict:
    plan = TurnPlan(**state["plan"])
    fallbacks = list(state.get("fallbacks") or [])
    status = final_status(error=bool(state["error"]), clarify=plan.intent == "ambiguous", refuse=plan.refuse,
                          action_unreleased=plan.intent == "access_request")
    if plan.intent == "ambiguous":
        text = plan.clarify_question or "Which one do you mean? Please give a name or system."
    elif status == "failed":
        text = f"I could not complete this: {state['error']}."
    else:
        ev_text, cut = _evidence(state["evidence"])
        if cut:
            fallbacks.append("evidence_truncated")
            get_client().update_current_span(level="WARNING", status_message=f"evidence shortened by {cut} characters")
        subj = ", ".join(x for x in (f"for {plan.subject_name}" if plan.subject_name else "",
                                     f"system {plan.system_name}" if plan.system_name else "") if x)
        body = (f"Today: {config['configurable'].get('as_of', '')[:10]}\n" + (f"This request is {subj}.\n" if subj else "") +
                f"Facts: {state.get('facts')}\nConstraints: {state.get('constraints')}\n"
                f"Recent conversation:\n{render(_window(state))}\n\n"
                f"Evidence:\n{ev_text}\n\nQuestion:\n{state['message']}")
        if state.get("handoff"):
            body += (f"\n\nA handoff to the Webex workflow was recorded as PENDING for {state['handoff']['subject_employee_id']} "
                     f"({state['handoff']['system']}). It is not filed, approved or granted.")
        text = llm.complete([{"role": "user", "content": body}], ANSWER_SYSTEM.format(status=status), max_tokens=700)
    h = state.get("handoff")
    if not answer_agrees(status, text):
        bad = claims_done(text)  # drop only the sentences that claim an action; keep the rest of the answer
        get_client().update_current_span(level="WARNING", status_message="answer disagreed with status: offending sentences dropped by code")
        _meta(guard_rewrote_answer=True, original_answer=text[:400], guard_dropped=bad)
        text = " ".join(x for x in re.split(r"(?<=[.!?])\s+", text) if x not in bad).strip()
        text = (text + " " if text else "") + "I have not filed or granted anything; that needs the recorded approvals first."
    if h and "pending" not in text.lower():  # the application, not the model, guarantees the honest outcome
        text += f" The {h['system']} access request for {h['subject_employee_id']} is pending; it is not filed, approved or in effect yet."
    dec = state.get("decision")
    if dec:  # the typed AccessDecision is built by code; its rendering is appended verbatim to the model's prose
        d = AccessDecision(**dec)
        status = d.status
        text += " " + d.render()
    _io({"evidence_items": len(state["evidence"]), "handoff": bool(h)}, {"answer": text[:400], "status": status})
    # The assistant's own sentences are never stored as facts: facts come from the user's words only.
    cl = checklist.build(state["evidence"])
    return {"answer": text, "status": status, "fallbacks": fallbacks, "checklist": cl.model_dump() if cl else None, "facts": _union(state.get("facts"), [f"{plan.intent}: {f}" for f in _row_facts(state["evidence"])]),
            "messages": [{"role": "user", "content": state["message"], "intent": plan.intent},
                         {"role": "assistant", "content": text, "intent": plan.intent}]}


async def pause(state: State, config) -> dict:
    """The durable pause (lesson 9). Approvals were dispatched this turn: persist and stop here. Whatever value
    resumes this node is ignored: only the approval rows, re-read through MCP, decide whether anything is released.
    The interrupt is an exception to the tracer, which would mark this span ERROR; a pause is a normal outcome."""
    if not state.get("pause_now"):
        with get_client().start_as_current_observation(name="pause", as_type="span", input={"pause_now": False}) as sp:
            sp.update(output={"paused": False})
        return {}
    lf = get_client()
    with lf.start_as_current_observation(name="pause", as_type="span", input={"pending": list(state["pending"])}) as sp:
        sp.update(output={"paused": True, "waiting_on": "recorded approval rows"})
    interrupt({"pending": list(state["pending"]), "message": "waiting for recorded approvals"})
    box = config["configurable"]["toolbox"]
    with lf.start_as_current_observation(name="pause.resume", as_type="span", input={"pending": list(state["pending"])}) as sp:
        pend, notes = dict(state["pending"]), []
        for rid, p in pend.items():
            if p["released"]:
                continue
            r = await release_pending(box, p)
            if r["released"]:
                pend[rid] = {**p, "released": True}
            notes.append(resume_text(rid, r))
        text = " ".join(notes)
        sp.update(output={"released": [k for k, v in pend.items() if v["released"]], "answer": text[:300]})
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
