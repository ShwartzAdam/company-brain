import hashlib
import os
import sys
from pathlib import Path

import pytest

os.environ["LANGFUSE_TRACING_ENABLED"] = "false"  # tests never send traces

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain import db, llm  # noqa: E402


class FakeBackend:
    """Deterministic: no AWS. embed is a hash vector, complete echoes a canned reply."""
    def __init__(self, reply="fake reply"):
        self.reply, self.calls = reply, []

    def complete(self, messages, system, max_tokens, temperature):
        self.calls.append((messages, system))
        return self.reply

    def embed(self, text):
        h = hashlib.sha256(text.encode()).digest()
        return [b / 255 for b in h] * 32


@pytest.fixture
def fake_llm():
    b = FakeBackend()
    llm.set_backend(b)
    yield b
    llm.set_backend(None)


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    from brain import config
    p = tmp_path / "t.db"
    monkeypatch.setattr(config, "DB_PATH", p)
    db.reset_db(p)
    return p
