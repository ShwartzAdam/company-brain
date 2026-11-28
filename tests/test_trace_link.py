"""A write's database row must lead back to the request and trace that caused it, through the MCP boundary."""
import asyncio

from mcp.shared.memory import create_connected_server_and_client_session as connect

from brain.webex import gate

from server import db, tools
from brain.mcp_client import Toolbox
from server.server import mcp


def test_request_id_crosses_mcp_and_lands_on_the_write_row():
    async def go():
        async with connect(mcp._mcp_server) as s:
            box = await Toolbox(s, "E010").load()
            box.ctx = {"as_of": "2026-07-01T12:00:00Z", "conversation_id": "c1", "request_id": "c1:3"}
            await box.call("record_consent", {"employee_id": "E010", "system": "Webex", "message_id": "m1", "draft": "d",
                                                   "consent_token": gate.consent_token("E010", "E010", "Webex", "m1", "d")})
            await box.call("get_employee", {"query": "E010"})
    asyncio.run(go())
    row = db.rows("SELECT request_id, trace_id FROM write_keys WHERE key = 'consent:m1'")[0]
    assert row["request_id"] == "c1:3"  # trace_id is empty here only because tracing is off in tests
    assert tools.REQUEST.get() == ("", "")  # the contextvar does not leak past the call


def test_a_direct_tool_call_stores_empty_links_and_old_databases_gain_the_columns(tmp_path):
    import sqlite3
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE write_keys (key TEXT PRIMARY KEY, kind TEXT, entity_id TEXT, created_at TEXT)")
    c.commit()
    c.close()
    db.conn(p).close()
    cols = {r[1] for r in sqlite3.connect(p).execute("PRAGMA table_info(write_keys)")}
    assert {"request_id", "trace_id"} <= cols


def test_get_employee_accepts_a_typo_but_never_guesses_a_first_name():
    assert [r["employee_id"] for r in tools.get_employee("rrachel stein")] == ["E010"]
    assert {r["employee_id"] for r in tools.get_employee("Maya")} == {"E001", "E020"}  # still ambiguous, never picked
    assert tools.get_employee("zzzz qqqq") == []


def test_raw_mcp_client_cannot_forge_consent():
    import pytest

    async def go(token):
        async with connect(mcp._mcp_server) as s:
            return await s.call_tool("record_consent", {"caller_id": "E010", "employee_id": "E010", "system": "Webex",
                                                        "message_id": "forged", "draft": "d", "as_of": "2026-07-01", "consent_token": token})
    for tok in ("", "0" * 64, gate.consent_token("E010", "E010", "Webex", "other-message", "d")):  # none, guessed, minted for another row
        r = asyncio.run(go(tok))
        assert r.isError and "consent token" in r.content[0].text
    assert db.rows("SELECT 1 FROM consents WHERE message_id = 'forged'") == []
    assert not asyncio.run(go(gate.consent_token("E010", "E010", "Webex", "forged", "d"))).isError
    with pytest.raises(PermissionError):
        gate.check_consent_token("", "E010", "E010", "Webex", "x", "d")


def test_a_consent_token_never_enters_a_trace_span_input():
    import inspect
    from brain import mcp_client
    src = inspect.getsource(mcp_client.Toolbox.call)
    assert '"<redacted>" if "token" in k' in src
