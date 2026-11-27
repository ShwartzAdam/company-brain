"""#18: a stuck or broken call ends ONE turn as `failed`; it never hangs or kills a run. No AWS, no live model."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
from brain import agent, config, graph, mcp_client, status  # noqa: E402
import run_submission as rs  # noqa: E402

AS_OF = "2026-07-01T12:00:00Z"


class HangingSession:
    specs = {}

    async def call_tool(self, name, args, meta=None):
        await asyncio.sleep(30)


def test_a_hung_tool_call_times_out(monkeypatch):
    monkeypatch.setattr(config, "TOOL_TIMEOUT_S", 0.1)
    box = mcp_client.Toolbox(HangingSession(), "E010")
    box.specs = {"get_employee": SimpleNamespace(inputSchema={"properties": {}})}
    with pytest.raises(TimeoutError, match="timed out"):
        asyncio.run(box.call("get_employee", {}))


class SlowGraph:
    def __init__(self, behaviour):
        self.behaviour = behaviour

    async def aget_state(self, cfg):
        return SimpleNamespace(values={})

    async def ainvoke(self, *a, **kw):
        return await self.behaviour()


def _turn(monkeypatch, tmp_path, behaviour):
    monkeypatch.setattr(graph, "build_graph", lambda saver: SlowGraph(behaviour))
    box = SimpleNamespace(ctx={})
    return asyncio.run(agent.ahandle_turn("E010", "UG_REGULAR", "t-fail", "hello", AS_OF, toolbox=box,
                                          checkpoint_path=tmp_path / "ck.db"))


def test_a_turn_that_hangs_comes_back_failed_not_stuck(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TURN_TIMEOUT_S", 0.2)

    async def hang():
        await asyncio.sleep(30)
    r = _turn(monkeypatch, tmp_path, hang)
    assert r.status == "failed" and "timed out" in r.error and r.request_id == "t-fail:1"
    assert status.answer_agrees(r.status, r.answer) and "nothing was changed" in r.answer


def test_a_turn_that_raises_comes_back_failed_not_raised(monkeypatch, tmp_path):
    async def boom():
        raise RuntimeError("tool exploded")
    r = _turn(monkeypatch, tmp_path, boom)
    assert r.status == "failed" and "tool exploded" in r.error


def test_run_all_goes_on_after_a_failed_turn_and_saves_after_every_turn():
    turns = [{"item": f"X-{i}", "workflow": "maya", "turn": None, "thread": f"X-{i}", "caller": "E010",
              "group": "UG_REGULAR", "message": "hi"} for i in range(3)]
    saved = []

    async def handle(caller, group, thread, msg, as_of, **kw):
        if thread == "X-1":
            raise RuntimeError("boom")
        if thread == "X-2":
            await asyncio.sleep(30)  # stuck: the backstop must end it
        return SimpleNamespace(trace_id="t", status="completed", request_id=f"{thread}:1", intent="other")

    rows = asyncio.run(rs.run_all(turns, handle, lambda: None, save=lambda r: saved.append(len(r)), turn_timeout=0.2))
    assert [r["status"] for r in rows] == ["completed", "failed", "failed"]
    assert "boom" in rows[1]["error"] and rows[2]["error"]
    assert saved == [1, 2, 3]  # written after each turn, so a crash keeps what finished


def test_bedrock_client_gets_explicit_timeouts(monkeypatch):
    import boto3
    from brain import llm
    seen = {}
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: seen.update(kw) or object())
    llm.BedrockBackend()
    c = seen["config"]
    assert c.connect_timeout == config.BEDROCK_CONNECT_TIMEOUT_S and c.read_timeout == config.BEDROCK_READ_TIMEOUT_S
    assert c.retries["max_attempts"] == config.BEDROCK_MAX_ATTEMPTS
