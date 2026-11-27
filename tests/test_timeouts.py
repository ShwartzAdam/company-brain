"""#18 follow-ups not covered by the PR #24 tests: the Bedrock client's explicit limits, and a broken resume is `failed`."""
import asyncio

import pytest

from brain import agent, config


def test_bedrock_client_gets_explicit_timeouts(monkeypatch):
    import boto3
    from brain import llm
    seen = {}
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: seen.update(kw) or object())
    llm.BedrockBackend()
    c = seen["config"]
    assert c.connect_timeout == config.BEDROCK_CONNECT_TIMEOUT_S and c.read_timeout == config.BEDROCK_READ_TIMEOUT_S
    assert c.retries["max_attempts"] == config.BEDROCK_MAX_ATTEMPTS


def test_a_resume_that_breaks_comes_back_failed_not_raised(monkeypatch, tmp_path):
    from types import SimpleNamespace

    async def boom(*a, **kw):
        raise RuntimeError("read_approval exploded")
    monkeypatch.setattr(agent, "_resume", boom)
    r = asyncio.run(agent.aresume_pending("E010", "UG_REGULAR", "no-such-thread", "2026-07-01T12:00:00Z",
                                          toolbox=SimpleNamespace(ctx={}), checkpoint_path=tmp_path / "ck.db"))
    assert r.status == "failed" and "exploded" in r.error
