"""The replay scorer itself must be right, or the baseline means nothing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import session_replay as sr  # noqa: E402

PATH = "documents/manager_playbook/promotion.md"


def test_forbidden_source_matches_golden_suffix_form():
    t = {"forbidden_sources": ["manager_playbook/promotion.md"]}
    assert sr.check_turn(t, "x", ["search_documents"], [PATH]) == [f"forbidden source {PATH}"]
    assert sr.check_turn(t, "x", [], ["documents/handbook/making-a-career.md"]) == []


def test_expected_source_basename_and_missing():
    t = {"expected_sources": ["making-a-career.md"]}
    assert sr.check_turn(t, "x", [], ["documents/handbook/making-a-career.md"]) == []
    assert sr.check_turn(t, "x", [], ["documents/policies/a.md"]) == ["expected source missing making-a-career.md"]


def test_facts_tools_and_write_tools():
    t = {"required_facts": ["7,500|7500"], "forbidden_facts": ["Datadog"], "should_use_tools": False,
         "forbidden_tools": ["list_employee_tickets"]}
    assert sr.check_turn(t, "limit 7500", [], []) == []
    f = sr.check_turn(t, "Datadog", ["create_ticket"], [])
    assert any("forbidden fact" in x for x in f) and any("missing fact" in x for x in f)
    assert "forbidden tool create_ticket" in f and any("tool use" in x for x in f)


def test_compare_flags_only_drops():
    old = {"S": {"1": {"runs": 3, "passes": 3}, "2": {"runs": 3, "passes": 1}}}
    new = {"S": {"1": {"runs": 3, "passes": 2, "fails": ["x"]}, "2": {"runs": 3, "passes": 3, "fails": []},
                 "3": {"runs": 3, "passes": 0, "fails": []}}}
    r = sr.compare(old, new)
    assert len(r) == 1 and r[0].startswith("S t1: 3/3 -> 2/3")


def test_eval_selftests_run_offline():
    import score_traces, webex_checks, checks  # noqa: E401
    checks._selftest()
    webex_checks._selftest()
    score_traces.selftest()
