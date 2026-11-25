"""The Maya checks must catch the failures they exist for, and pass a correct run."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import maya_checks as mc  # noqa: E402

S2, S3 = mc.S2, mc.S3


def rec(session, n, answer="", tools=(), sources=(), extra=None):
    return {"session": session, "n": n, "answer": answer, "tools": list(tools), "sources": list(sources),
            "status": "completed", "extra": extra or {}}


def test_carry_fails_when_constraint_lost_and_passes_when_present():
    bad = [rec(S2, 9, "A seat costs money.", ["search_documents"])]
    assert any("7,500" in f for f in mc.check_carry(bad))
    good = [rec(S2, 9, "Under the Q3 freeze, Finance must approve: threshold 7,500, Webex costs 18,400.")]
    assert [f for f in mc.check_carry(good) if f.startswith(f"{S2} t9")] == []
    assert any("used tools" in f for f in mc.check_carry([rec(S2, 7, "Israel, 2026-08-01", ["get_employee"])]))


def test_closed_branch_tool_and_word_leaks_are_caught():
    f = mc.check_closed([rec(S2, 5, "x", ["list_employee_tickets"]), rec(S2, 12, "Rachel's T001 is open")])
    assert any("closed-branch tool" in x for x in f) and any("'Rachel'" in x for x in f)
    assert any("durable fact" in x for x in mc.check_closed([rec(S3, 8, "I do not remember.")]))
    assert not any("durable fact" in x for x in mc.check_closed([rec(S3, 8, "It was Rachel Stein.")]))


def test_citations_require_a_source_and_reject_invented_ones():
    src = "documents/policies/byod_policy.md"
    assert mc.check_citations([rec(S2, 2, "Five days [documents/policies/byod_policy.md].", ["search_documents"], [src])]) == []
    assert any("cites nothing" in f for f in mc.check_citations([rec(S2, 2, "Five days.", ["search_documents"], [src])]))
    assert mc.check_citations([rec(S2, 3, "OT005 is blocked.", ["list_onboarding_tasks"])]) == []
    inv = mc.check_citations([rec(S2, 2, "See [documents/policies/made_up.md].", ["search_documents"], [src])])
    assert any("never retrieved" in f for f in inv)
    assert mc.check_citations([rec(mc.S9, 4, "Not available.", ["search_documents"])]) == []  # refusal exempt


def test_writes_flag_write_tool_and_changed_tables():
    same = {"tickets": "a"}
    assert mc.check_writes([rec(S2, 1)], {S2: same}, {S2: same}) == []
    f = mc.check_writes([rec(S2, 10, tools=["create_access_request"])], {S2: same}, {S2: {"tickets": "b"}})
    assert any("write tool" in x for x in f) and any("tickets changed" in x for x in f)


def good_handoff(**kw):
    h = {"subject_employee_id": "E001", "system": "Webex", "justification": "start 2026-08-01",
         "conversation_id": "S2", "caller_id": "E004", "idempotency_key": "E001:webex:S2"}
    return {"handoff": {**h, **kw}, "handoff_calls": 1}


def test_handoff_exactly_once_right_subject_and_replay_safe():
    recs = [rec(S2, 10, extra=good_handoff())]
    assert mc.check_handoff(recs, rec(S2, 13, extra={"handoff_calls": 1})) == []
    assert any("E020" in f for f in mc.check_handoff([rec(S2, 10, extra=good_handoff(subject_employee_id="E020"))],
                                                    rec(S2, 13, extra={"handoff_calls": 1})))
    assert any("replay" in f for f in mc.check_handoff(recs, rec(S2, 13, extra={"handoff_calls": 2})))
    assert any("no typed handoff" in f for f in mc.check_handoff([rec(S2, 10)], None))
    assert any("did not ask" in f for f in mc.check_handoff(recs + [rec(S2, 9, extra=good_handoff())], rec(S2, 13, extra={"handoff_calls": 1})))


def test_restart_needs_early_facts_without_tools():
    assert mc.check_restart(rec(S2, 7, "Israel, starts 2026-08-01")) == []
    assert mc.check_restart(rec(S2, 7, "I do not know")) != []
    assert mc.check_restart(None) != []


def test_faithful_flags_low_score_and_accumulates_session_evidence():
    seen = []

    def judge(msgs, ev, ans):
        seen.append((len(msgs), len(ev)))
        return (0.2, ["made up"]) if "invented" in ans else (1.0, [])
    recs = [dict(rec(S2, 1, "ok"), user="a", evidence=[{"tool": "t", "args": {}, "result": []}]),
            dict(rec(S2, 2, "invented fact"), user="b", evidence=[])]
    f = mc.check_faithful(recs, judge)
    assert len(f) == 1 and "t2" in f[0] and seen == [(1, 1), (2, 1)]  # t2 still sees t1's evidence


def test_args_check_catches_wrong_employee_and_ignores_uncalled_tools():
    import session_replay as sr
    t = {"arg_checks": [{"tool": "list_onboarding_tasks", "arg": "employee_id", "equals": "E001"},
                        {"tool": "get_employee", "arg": "query", "contains": "maya"}]}
    assert sr.check_args(t, [{"tool": "list_onboarding_tasks", "args": {"employee_id": "E020"}}]) != []
    ok = [{"tool": "list_onboarding_tasks", "args": {"employee_id": "E001"}},
          {"tool": "get_employee", "args": {"query": "Maya Cohen"}}]
    assert sr.check_args(t, ok) == [] and sr.check_args(t, []) == []
