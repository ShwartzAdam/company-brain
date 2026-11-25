import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import judge  # noqa: E402


def test_parse_verdict_and_garbage_fails_closed():
    assert judge.parse_verdict('ok {"score": 0.5, "unsupported": ["x"]}') == (0.5, ["x"])
    assert judge.parse_verdict("no idea")[0] == 0.0
    assert judge.parse_verdict('{"score": 7}')[0] == 1.0


def test_judge_sends_evidence_and_user_messages(scripted):
    b = scripted('{"score": 1.0, "unsupported": []}')
    score, bad = judge.faithfulness(["is a seat free?"], [{"tool": "check_software_subscription", "args": {"software": "Webex"},
                                                           "result": [{"source": "db:software_subscriptions/SUB001"}]}], "No [db:software_subscriptions/SUB001]")
    body = b.calls[0][0][0]["content"]
    assert score == 1.0 and "SUB001" in body and "is a seat free?" in body
