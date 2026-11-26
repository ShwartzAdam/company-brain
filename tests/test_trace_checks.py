"""trace_checks must pass a followable trace and name what is missing from one that is not. Observations here are
shaped like Langfuse v2 rows; the real shape was checked against live traces."""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import checks  # noqa: E402
import trace_checks as tc  # noqa: E402

RID = "t1:1"
BASE = {"caller_id": "E010", "caller_group": "UG_REGULAR", "request_id": RID, "as_of": "2026-07-01", "case_id": "W-I-03",
        "turn": "1", "scope": "webex", "intent": "access_request", "visible_tools": ["get_employee"],
        "tool_sequence": ["check_software_subscription"], "status": "pending"}


def ob(id, name, t, parent="root", type="SPAN", md=None, **kw):
    return {"id": id, "name": name, "start_time": f"2026-07-01T12:00:{t:02d}", "parent_observation_id": parent, "type": type,
            "metadata": md if md is not None else {"request_id": RID}, **kw}


def good():
    return [
        ob("root", "agent.turn", 0, None, "AGENT", dict(BASE), output="Webex is over its seat limit; no access was given."),
        ob("c", "classify", 1), ob("g1", "llm.structured:classify", 2, "c", "GENERATION", {}, model="m", usage_details={"total": 9}),
        ob("s", "scope", 3), ob("ga", "gather", 4),
        ob("t", "tool.check_software_subscription", 5, "ga", "TOOL"),
        ob("sv", "server.check_software_subscription", 6, "t", "TOOL"),
        ob("an", "answer", 7)]


def test_followable_trace_passes_and_summary_reads():
    assert tc.verdict(good()) == ("PASS", [])
    assert "E010/UG_REGULAR webex:access_request" in tc.summary(good())


def mutate(fn):
    o = copy.deepcopy(good())
    fn(o)
    return tc.verdict(o)


def test_each_missing_piece_is_named():
    v, f = mutate(lambda o: o[0]["metadata"].pop("caller_group"))
    assert v == "FAIL" and any("caller_group" in x for x in f)
    assert any("out of order" in x for x in mutate(lambda o: o[3].update(start_time="2026-07-01T12:00:00"))[1])  # scope before classify
    assert any("no tool.check_software_subscription" in x for x in mutate(lambda o: o.pop(5))[1])
    assert any("request_id" in x for x in mutate(lambda o: o[5]["metadata"].update(request_id="other"))[1])
    assert any("no model" in x for x in mutate(lambda o: o[2].pop("model"))[1])
    assert any("disagree" in x for x in mutate(lambda o: o[0].update(output="I have filed the request."))[1])
    assert any("not one of the five" in x for x in mutate(lambda o: o[0]["metadata"].update(status="done"))[1])


def test_write_needs_a_linked_server_span_and_order():
    def add_write(o):
        o += [ob("w", "tool.create_access_request", 8, "ga", "TOOL")]
    f = mutate(add_write)[1]
    assert any("no server.create_access_request" in x and "linked" in x for x in f)
    assert any("before record_consent" in x for x in f)


def test_maya_scope_with_a_write_span_fails():
    def f(o):
        o[0]["metadata"]["scope"] = "maya"
        o += [ob("w", "tool.record_consent", 8, "ga", "TOOL"), ob("ws", "server.record_consent", 9, "w", "TOOL")]
    assert any("Maya's scope" in x for x in mutate(f)[1])


def test_unreadable_trace_is_invalid_not_failed():
    assert tc.verdict([])[0] == "INVALID"
    o = copy.deepcopy(good())
    o[0]["metadata"].pop("status")
    assert tc.verdict(o)[0] == "INVALID"


def test_silent_classify_fallback_must_be_flagged_on_the_root():
    def f(o):
        o[1]["metadata"]["classify_fallback"] = True
    assert any("default plan" in x for x in mutate(f)[1])
    def ok(o):
        o[1]["metadata"]["classify_fallback"] = True
        o[0]["metadata"]["fallbacks"] = ["classify_default_plan"]
    assert mutate(ok)[0] == "PASS"


def test_resume_must_name_the_request_it_resumes():
    r = [ob("root", "agent.resume", 0, None, "AGENT", {"caller_id": "E010", "request_id": RID, "resume_of": RID, "status": "needs_human"},
            output="Released to IT; no seat assigned.")]
    assert tc.verdict(r) == ("PASS", [])
    r[0]["metadata"]["resume_of"] = "t0:9"
    assert tc.verdict(r)[0] == "FAIL"


def test_write_order_and_fact_stage():
    assert tc.write_order(["read_approval"]) is None
    assert tc.write_order(["record_consent", "create_access_request", "request_approval"])[0] == 1.0
    assert tc.write_order(["request_approval"])[0] == 0.0
    assert tc.write_order(["record_consent", "release_access"])[0] == 0.0
    assert tc.write_order(["read_approval", "release_access"])[0] == 1.0
    assert checks.fact_stage("E010", "x E010 y", "Rachel has a ticket") == ("answer", "'E010' was in the evidence but the answer left it out")
    assert checks.fact_stage("E010", "nothing", "Rachel")[0] == "gather"
    assert checks.fact_stage("E010", "nothing", "Rachel", intent_ok=False)[0] == "classify"
    assert checks.fact_stage("E010", "", "id E010") is None
