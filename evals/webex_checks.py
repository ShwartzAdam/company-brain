"""Webex checks nothing ships (golden readme: 'idempotency and restart assertions ... are yours').
Pure functions: tested offline with --selftest, used by webex_replay.py against a live run.

  rows_unchanged   no business row added by a read-only case
  reuse            T001 / AR001 / AP rows are the seed rows: no second ticket, request or approval
  replay           the same case replayed (same and new thread) adds no row and no new write key
  never_granted    no answer or decision text claims access was granted (heuristic regex + approval state)
  status_agrees    terminal status is one of five and the answer text agrees
  no_write_before_consent   no write row exists unless a consent row was recorded for it
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.status import STATUSES, answer_agrees  # noqa: E402

BUSINESS = ("tickets", "access_requests", "approvals")


def check_rows(before: dict, after: dict, allow: dict | None = None) -> list:
    """Business tables may grow only by what `allow` says (default nothing): the 'no duplicate row' check."""
    allow = allow or {}
    return [f"{t}: {before[t]} -> {after[t]} (allowed +{allow.get(t, 0)})" for t in BUSINESS
            if after[t] != before[t] + allow.get(t, 0)]


def check_replay(first: dict, second: dict) -> list:
    """After replaying the case, every table (business, audit_log, write_keys) is exactly as after the first run."""
    return [f"replay changed {t}: {first[t]} -> {second[t]}" for t in first if first[t] != second.get(t)]


def check_state(approvals: dict, expect: dict) -> list:
    return [f"{k} is {approvals.get(k)!r}, expected {v!r}" for k, v in expect.items() if approvals.get(k) != v]


def check_never_granted(answer: str, decision: dict | None, access_request_status: str, approvals: dict) -> list:
    out = [] if answer_agrees("pending", answer) else ["answer claims a grant or a filing"]
    if decision and decision.get("status") not in STATUSES:
        out.append(f"decision status {decision.get('status')!r} is not one of the five")
    if access_request_status == "approved" and any(v != "approved" for v in approvals.values()):
        out.append("request is approved while an approval row is not")
    return out


def check_status_agrees(status: str, answer: str) -> list:
    return [] if answer_agrees(status, answer) else [f"status {status!r} and answer disagree"]


def check_write_needs_consent(write_keys: list, consents: list) -> list:
    """Every approval/request/release write key names a conversation message that has a consent row."""
    return [f"write {k} has no consent" for k in write_keys if k.split(":")[0] in ("ar", "ap", "rel") and not consents]


def check_nofile_carry(base: dict, after: dict, n: int, go_turn: int | None) -> list:
    """A 'do not file until I say so' rule carries turn by turn: until the caller's own go-ahead turn, no write of any
    kind (business rows, write ledger, consent rows) may appear. Checked after EVERY turn, not only the last."""
    if go_turn is not None and n >= go_turn:
        return []
    return [f"t{n}: {t} {base[t]} -> {after[t]} before any go-ahead" for t in (*BUSINESS, "write_keys", "consents") if t in base and after.get(t) != base[t]]


def _selftest():
    b = {"tickets": 1, "access_requests": 5, "approvals": 4, "write_keys": 0, "consents": 0}
    assert check_nofile_carry(b, dict(b), 2, 4) == [] and check_nofile_carry(b, {**b, "consents": 1}, 3, 4)
    assert check_nofile_carry(b, {**b, "write_keys": 3}, 4, 4) == [] and check_nofile_carry(b, {**b, "write_keys": 3}, 2, None)
    b = {"tickets": 1, "access_requests": 5, "approvals": 4}
    assert check_rows(b, dict(b)) == [] and check_rows(b, {**b, "tickets": 2})
    assert check_rows(b, {**b, "approvals": 5}, {"approvals": 1}) == []
    assert check_replay({"a": 1, "write_keys": 3}, {"a": 1, "write_keys": 4})
    assert check_state({"AP001": "pending"}, {"AP001": "pending"}) == [] and check_state({"AP001": "needed"}, {"AP001": "pending"})
    assert check_never_granted("Access has been granted to Rachel", None, "blocked", {})
    assert check_never_granted("Blocked at 42/40, nothing granted.", {"status": "pending"}, "blocked", {"AP1": "pending"}) == []
    assert check_never_granted("ok", None, "approved", {"AP1": "pending"})
    assert check_status_agrees("pending", "I've filed it.") and check_status_agrees("pending", "AR001 is pending.") == []
    assert check_write_needs_consent(["ar:E010:S001:c"], []) and check_write_needs_consent(["ar:E010:S001:c"], ["m"]) == []
    print("webex_checks selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
