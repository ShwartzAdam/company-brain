"""The submission run's plumbing, offline: parse, count, threads, group mapping, the table fill, and the full dry path."""
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evals"))
import run_submission as rs  # noqa: E402

SAMPLE = textwrap.dedent("""
    workflows:
    - id: maya
      required: true
      inputs:
      - id: M-I-01
        caller: {employee_id: E010, name: Rachel, group: Regular Employee}
        message: Can I use my laptop?
      sessions:
      - id: M-S-01
        caller: {employee_id: E004, name: Sara, group: HR Group}
        turns:
        - {n: 1, message: hello}
        - {n: 2, message: again}
    - id: vendor
      required: false
      inputs:
      - id: V-I-01
        source_name: x
    """)


def test_load_flattens_inputs_and_sessions_in_order(tmp_path):
    p = tmp_path / "e.yaml"
    p.write_text(SAMPLE)
    t = rs.load(p)
    assert [(x["item"], x["turn"], x["thread"], x["group"]) for x in t] == [
        ("M-I-01", None, "M-I-01", "UG_REGULAR"), ("M-S-01", 1, "M-S-01", "UG_HR"), ("M-S-01", 2, "M-S-01", "UG_HR")]
    assert rs.counts(t) == {"maya": 3}


def test_fill_writes_only_blank_rows_and_only_for_known_ids():
    md = "| `M-I-01` | — | |\n| `M-S-01` | 2 | |\n| `M-S-01` | 3 | keep |\n| `W-I-01` | — | |\nother | text |"
    out = rs.fill(md, {("M-I-01", None): "abc", ("M-S-01", 2): "def", ("M-S-01", 3): "zzz"})
    assert "| `M-I-01` | — | abc |" in out and "| `M-S-01` | 2 | def |" in out
    assert "| keep |" in out and "| `W-I-01` | — | |" in out and out.endswith("other | text |")


YAML_PRESENT = rs.YAML.exists()


@pytest.mark.skipif(not YAML_PRESENT, reason="data/ (the dataset copy) is not committed")
def test_the_real_set_is_27_and_a_dry_run_replays_all_of_it_on_the_right_threads():
    t = rs.load()
    assert rs.counts(t) == rs.EXPECTED and len(t) == 27
    rows = rs.dry_run(t)
    assert len(rows) == 27 and all(r["trace_id"] and r["status"] for r in rows)
    sess = [r for r in rows if r["item"] == "M-S-01"]
    assert len(sess) == 12 and len({r["thread"] for r in sess}) == 1  # one thread, so turn 12 can see turn 1
    assert len({r["thread"] for r in rows if r["turn"] is None}) == 6  # every one-shot input starts its own thread


def test_run_all_survives_a_hung_and_a_raising_turn_and_saves_after_every_turn():
    import asyncio
    from types import SimpleNamespace
    turns = [rs._turn("maya", f"M-I-0{i}", None, {"employee_id": "E010", "group": "Regular Employee"}, "hi") for i in range(1, 4)]
    saved = []

    async def handle(caller, group, thread, msg, as_of, **kw):
        if thread == "M-I-01":
            await asyncio.sleep(30)
        if thread == "M-I-02":
            raise RuntimeError("boom")
        return SimpleNamespace(trace_id="abcdef123", status="completed", intent="other", request_id="r")

    rows = asyncio.run(rs.run_all(turns, handle, lambda: None, save=lambda r: saved.append(len(r)), timeout=0.2))
    assert saved == [1, 2, 3]  # written after every turn, not once at the end
    assert [r["status"] for r in rows] == ["failed", "failed", "completed"]
    assert rows[0]["error"] == "timeout" and "boom" in rows[1]["error"]
