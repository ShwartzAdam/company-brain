"""Regression checks for review findings #1, #3, #4, #5, #6, #8 (pure string logic, no model)."""
from brain.claims import ground
from brain.claims import clean_citations
from brain.evidence import ambiguous_first_name
from brain.status import claims_done

E = "db:employees/E010"


def test_negation_across_commas_is_honest():  # 1
    assert claims_done("Nothing, including the Webex request, has been filed.") == []
    assert claims_done("No Webex access for Maya (E010, Engineering) is active.") == []
    assert claims_done("Nothing is pending, but access has been granted.")  # 'but' still opens a new clause


def _emp(q, *rows):
    return {"tool": "get_employee", "args": {"query": q},
            "result": [{"employee_id": i, "full_name": n, "source": f"db:employees/{i}"} for i, n in rows]}


def test_ambiguous_first_name_resolution():  # 3
    dans = _emp("Dan", ("E001", "Dan Levi"), ("E002", "Dan Cohen"))
    assert ambiguous_first_name([dans, _emp("Jordan Smith", ("E009", "Jordan Smith"))])  # substring 'dan' no longer hides it
    assert ambiguous_first_name([dans, _emp("E002", ("E002", "Dan Cohen"))]) is None   # id lookup resolves it
    assert ambiguous_first_name([dans, _emp("Dan Cohen", ("E002", "Dan Cohen"))]) is None


def test_ground_keeps_layout():  # 4
    text = "Status:\n- AR001 pending\n- T001 open\n\nNext steps follow."
    out, _ = ground(text, {"db:access_requests/AR001", "db:tickets/T001"})
    assert out == "Status:\n- AR001 pending [db:access_requests/AR001]\n- T001 open [db:tickets/T001]\n\nNext steps follow."


def test_ground_no_duplicate_trailing_citation():  # 5
    out, typed = ground(f"Maya Cohen is E010. [{E}] She starts Monday.", {E})
    assert out == f"Maya Cohen is E010. [{E}] She starts Monday."
    assert not any(c.added_by_code for c in typed.claims)


def test_ground_accepts_fragment_citations():  # 6
    doc = "documents/policies/byod_policy.md"
    out, typed = ground(f"E010 must enrol MFA [{doc}#mfa].", {doc, E})
    assert E not in out and not typed.uncited


def test_none_is_not_a_source():  # 8
    out, dropped = clean_citations("X [None].", {E})
    assert dropped == ["None"]
