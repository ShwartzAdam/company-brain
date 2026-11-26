import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import calibrate_judges  # noqa: E402
import judge  # noqa: E402

CASES = json.loads((Path(__file__).resolve().parent.parent / "evals" / "judge_calibration.json").read_text())


def test_garbage_verdict_fails_closed(scripted):
    scripted("no idea", "no idea")  # the structured call retries once, then the judge scores 0 with a reason
    s, flagged = judge.faithfulness(["q"], [], "an answer")
    assert s == 0.0 and "no valid verdict" in flagged[0]


def test_faithfulness_sends_evidence_and_user_messages(scripted):
    b = scripted('{"score": 1.0, "flagged": [], "reasoning": "ok"}')
    score, bad = judge.faithfulness(["is a seat free?"], [{"tool": "check_software_subscription", "args": {"software": "Webex"},
                                                           "result": [{"source": "db:software_subscriptions/SUB001"}]}], "No [db:software_subscriptions/SUB001]")
    body = b.calls[0][0][0]["content"]
    assert score == 1.0 and "SUB001" in body and "is a seat free?" in body


def test_overstates_is_zero_without_a_model_when_the_regex_proves_it(scripted):
    b = scripted('{"score": 1.0, "flagged": [], "reasoning": "x"}')
    s, flagged, why = judge.overstates("I have filed the request.", {"status": "pending", "actions_taken": []})
    assert s == 0.0 and not b.calls and "deterministic" in why


def test_overstates_sends_code_recorded_truth(scripted):
    b = scripted('{"score": 0.5, "flagged": ["AR001 is approved"], "reasoning": "approvals only requested"}')
    s, flagged, _ = judge.overstates("AR001 is approved.", {"status": "pending", "actions_taken": ["requested approvals AP001"]})
    assert s == 0.5 and flagged == ["AR001 is approved"] and "requested approvals AP001" in b.calls[0][0][0]["content"]


def test_routing_by_what_the_turn_did():
    assert judge.judges_for({"tools": [], "extra": {}}) == []
    assert judge.judges_for({"tools": ["get_employee"], "extra": {}}) == ["faithfulness"]
    assert judge.judges_for({"tools": ["x"], "extra": {"decision": {"status": "pending"}}}) == ["faithfulness", "overstates"]
    t = judge.truth_of({"status": "pending", "extra": {"decision": {"actions_taken": ["a"]}, "handoff_calls": 1}})
    assert t["actions_taken"] == ["a"] and t["handoff_calls"] == 1


def test_calibration_runner_reports_a_judge_that_cannot_separate():
    always_high = {"overstates": lambda c: (1.0, []), "faithfulness": lambda c: (1.0, [])}
    bad = calibrate_judges.run(CASES, always_high)
    assert "overstates/claims-filed" in bad and "faithfulness/invented-number" in bad  # a lenient judge is caught
    perfect = {"overstates": lambda c: (0.0 if c["expect_low"] else 1.0, []), "faithfulness": lambda c: (0.0 if c["expect_low"] else 1.0, [])}
    assert calibrate_judges.run(CASES, perfect) == []


def test_planted_overclaims_trip_the_deterministic_floor():
    from brain.status import answer_agrees
    assert not answer_agrees("pending", CASES["overstates"][0]["answer"].replace("submitted the Webex request for Maya", "filed it"))
