import ast
from pathlib import Path

import pytest

from brain import db, ingest, people, retrieval, tools

ROOT = Path(__file__).resolve().parent.parent
AS_OF = "2026-07-01T12:00:00Z"


def test_llm_is_only_bedrock_importer():
    bad = []
    for p in list((ROOT / "brain").glob("*.py")) + list((ROOT / "server").glob("*.py")) + [ROOT / "scripts/ask.py"]:
        if p.name == "llm.py":
            continue
        t = p.read_text()
        if "bedrock-runtime" in t or "invoke_model" in t or ".converse(" in t:
            bad.append(p.name)
    assert not bad


def test_db_facts():
    assert len(db.rows("SELECT 1 FROM contracts")) == 20
    assert {r["employee_id"] for r in tools.get_employee("Maya")} >= {"E001", "E020"}
    st = {r["approval_id"]: r["status"] for r in tools.read_approval("AR001")}
    assert st == {"AP001": "needed", "AP002": "needed"}
    assert db.rows("SELECT 1 FROM tickets WHERE ticket_id='T001'")
    assert tools.get_contract("Webex")[0]["document_path"]


def test_manager_rule():
    assert people.is_manager("E018") and not people.is_manager("E010")
    assert {r["employee_id"] for r in tools.list_direct_reports("E018")}


def test_ingest_counts_and_audience():
    recs = ingest.load_chunks()
    assert len({r["source"] for r in recs}) == 102
    assert {r["corpus"] for r in recs} == {"contracts", "employment", "handbook", "internal_memos",
                                           "it_kb", "manager_playbook", "policies"}
    assert all(r["audience"] == "manager" for r in recs if r["corpus"] == "manager_playbook")
    assert all(r["audience"] == "all" for r in recs if r["corpus"] != "manager_playbook")
    assert all(r["text"].strip() for r in recs)
    kb = [r for r in recs if r["source"].endswith("mfa_reset.md")]
    assert kb and all(r["title"] in r["text"] for r in kb)


def test_unknown_audience_fails(tmp_path, monkeypatch):
    from brain import config
    d = tmp_path / "documents" / "policies"
    d.mkdir(parents=True)
    (d / "x.md").write_text("---\naudience: managr\n---\n# X\nbody")
    monkeypatch.setattr(config, "DOCS_DIR", tmp_path / "documents")
    with pytest.raises(ValueError):
        ingest.load_chunks()


def test_filter_is_in_query_body(fake_llm):
    emp = retrieval.build_body("promotion", "E010", 5, None)["query"]["knn"]["vector"]
    mgr = retrieval.build_body("promotion", "E018", 5, None)["query"]["knn"]["vector"]
    assert {"term": {"audience": "all"}} in emp["filter"]["bool"]["must"]
    assert "filter" not in mgr


def test_access_request_reuses_and_is_idempotent():
    a = tools.create_access_request("E018", "E010", "Webex", "calls", "c1", AS_OF)
    assert a["entity_id"] == "AR001" and a["reused"]
    b = tools.create_access_request("E018", "E010", "Webex", "calls", "c1", AS_OF)
    assert b["replayed"]
    n = len(db.rows("SELECT 1 FROM access_requests"))
    tools.create_access_request("E018", "E010", "Webex", "calls", "c2", AS_OF)
    assert len(db.rows("SELECT 1 FROM access_requests")) == n


def test_new_request_twice_one_row():
    n = len(db.rows("SELECT 1 FROM access_requests"))
    tools.create_access_request("E001", "E001", "Okta", "start", "c1", AS_OF)
    tools.create_access_request("E001", "E001", "Okta", "start", "c1", AS_OF)
    assert len(db.rows("SELECT 1 FROM access_requests")) == n + 1


def test_ticket_reuses_t001():
    r = tools.create_ticket("E010", "E010", "Access", "Webex license", "no license", "d", "Webex", "c1", AS_OF)
    assert r["entity_id"] == "T001" and r["reused"]


def test_approval_request_transitions_once():
    r = tools.request_approval("E018", "AP001", "c1", AS_OF)
    assert r["status"] == "pending"
    tools.request_approval("E018", "AP001", "c1", AS_OF)
    assert len(db.rows("SELECT 1 FROM audit_log WHERE event_type='approval_requested'")) == 1
    assert len(db.rows("SELECT 1 FROM approvals")) == 4


def test_cross_user_write_denied():
    with pytest.raises(PermissionError):
        tools.create_access_request("E010", "E001", "Okta", "x", "c1", AS_OF)


def test_survives_restart():
    tools.request_approval("E018", "AP002", "c1", AS_OF)
    assert {r["approval_id"]: r["status"] for r in tools.read_approval("AR001")}["AP002"] == "pending"


def test_single_write_release():
    """Only release_write commits; no other function in brain/tools.py or server/ does."""
    for p in [ROOT / "brain/tools.py", ROOT / "server/server.py"]:
        for fn in [n for n in ast.walk(ast.parse(p.read_text())) if isinstance(n, ast.FunctionDef)]:
            src = ast.get_source_segment(p.read_text(), fn)
            if fn.name != "release_write":
                assert ".commit()" not in src, fn.name


def test_mcp_server_lists_tools():
    import server.server as s
    names = {t.name for t in s.mcp._tool_manager.list_tools()}
    assert {"get_contract", "list_direct_reports", "read_approval", "create_ticket",
            "request_approval", "search_documents"} <= names


def test_ask_answer_uses_one_llm_call(fake_llm):
    import importlib.util
    spec = importlib.util.spec_from_file_location("ask", ROOT / "scripts/ask.py")
    ask = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ask)
    out = ask.answer("q?", [{"source": "documents/policies/equipment_policy.md", "text": "five days"}])
    assert out == "fake reply" and len(fake_llm.calls) == 1
    assert "equipment_policy.md" in fake_llm.calls[0][0][0]["content"]
