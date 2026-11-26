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


def consent(caller, emp, system="Webex", mid="m1"):
    tools.record_consent(caller, emp, system, mid, "draft", AS_OF)
    return mid


def test_access_request_reuses_and_is_idempotent():
    consent("E018", "E010")
    a = tools.create_access_request("E018", "E010", "Webex", "calls", "c1", AS_OF, "m1")
    assert a["entity_id"] == "AR001" and a["reused"]
    b = tools.create_access_request("E018", "E010", "Webex", "calls", "c1", AS_OF, "m1")
    assert b["replayed"]
    n = len(db.rows("SELECT 1 FROM access_requests"))
    tools.create_access_request("E018", "E010", "Webex", "calls", "c2", AS_OF, "m1")
    assert len(db.rows("SELECT 1 FROM access_requests")) == n


def test_new_request_twice_one_row():
    consent("E001", "E001", "Okta")
    n = len(db.rows("SELECT 1 FROM access_requests"))
    tools.create_access_request("E001", "E001", "Okta", "start", "c1", AS_OF, "m1")
    tools.create_access_request("E001", "E001", "Okta", "start", "c1", AS_OF, "m1")
    assert len(db.rows("SELECT 1 FROM access_requests")) == n + 1


def test_ticket_reuses_t001():
    r = tools.create_ticket("E010", "E010", "Access", "Webex license", "no license", "d", "Webex", "c1", AS_OF)
    assert r["entity_id"] == "T001" and r["reused"]


def test_approval_request_transitions_once():
    consent("E010", "E010")
    r = tools.request_approval("E010", "AP001", "m1", "c1", AS_OF)
    assert r["status"] == "pending"
    tools.request_approval("E010", "AP001", "m1", "c1", AS_OF)
    assert len(db.rows("SELECT 1 FROM audit_log WHERE event_type='approval_requested'")) == 1
    assert len(db.rows("SELECT 1 FROM approvals")) == 4


def test_cross_user_write_denied():
    consent("E010", "E001", "Okta")
    with pytest.raises(PermissionError):
        tools.create_access_request("E010", "E001", "Okta", "x", "c1", AS_OF, "m1")


def test_survives_restart():
    consent("E010", "E010")
    tools.request_approval("E010", "AP002", "m1", "c1", AS_OF)
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


def test_keyword_query_carries_audience_filter(fake_llm):
    body = retrieval.build_keyword_body("promotion", "E010", 5, None)
    assert {"term": {"audience": "all"}} in body["query"]["bool"]["filter"]
    assert retrieval.build_keyword_body("promotion", "E018", 5, None)["query"]["bool"]["filter"] == []


def test_rrf_merges_and_dedups():
    a = [{"_id": "1"}, {"_id": "2"}]
    b = [{"_id": "2"}, {"_id": "3"}]
    assert [h["_id"] for h in retrieval.rrf([a, b])] == ["2", "1", "3"]


def _hits(n):
    return [{"_source": {"source": f"s{i}", "text": "t"}} for i in range(n)]


def test_rerank_orders_and_fills_from_fused(fake_llm):
    fake_llm.reply = "Best: [2, 0]"
    out = retrieval.rerank("q", _hits(4), 3)
    assert [h["_source"]["source"] for h in out] == ["s2", "s0", "s1"]


def test_rerank_garbage_falls_back_and_ignores_bad_ids(fake_llm):
    fake_llm.reply = "no idea"
    assert [h["_source"]["source"] for h in retrieval.rerank("q", _hits(3), 2)] == ["s0", "s1"]
    fake_llm.reply = "[9, 1]"
    assert retrieval.rerank("q", _hits(3), 2)[0]["_source"]["source"] == "s1"
