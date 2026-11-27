"""#21: the Webex checks run in pytest (not only by hand with --selftest), and each can fail for the case it exists for."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import webex_checks as wc  # noqa: E402


def test_the_selftest_passes():
    wc._selftest()


def test_a_consent_row_from_another_conversation_or_person_does_not_satisfy_a_write():
    key = ["ar:E010:SYS001:conv-a"]
    assert wc.check_write_needs_consent(key, [{"message_id": "conv-a:1", "employee_id": "E010"}]) == []
    assert wc.check_write_needs_consent(key, [{"message_id": "conv-b:1", "employee_id": "E010"}])
    assert wc.check_write_needs_consent(key, [{"message_id": "conv-a:1", "employee_id": "E011"}])
    assert wc.check_write_needs_consent(["rel:AR001:conv-a"], [])
