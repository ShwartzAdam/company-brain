"""Replay golden sessions turn by turn on one thread and check the BINDING fields only
(required_facts, forbidden_tools, should_use_tools, forbidden_sources). Live Bedrock + MCP server.
python evals/session_replay.py S1-quick-policy [S3-intent-shift ...]"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from langfuse import get_client  # noqa: E402

from brain import config, db  # noqa: E402
from brain.agent import handle_turn  # noqa: E402
from brain.scope import WRITE_TOOLS  # noqa: E402

AS_OF = "2026-07-01T12:00:00Z"
CALLERS = {"S1-quick-policy": ("E010", "UG_REGULAR"), "S3-intent-shift": ("E007", "UG_IT"),
           "S4-constraint-carry": ("E006", "UG_IT")}


def has(text: str, spec: str) -> bool:
    return any(alt.strip().lower() in text.lower() for alt in spec.split("|"))


def main(ids):
    data = json.loads((config.ROOT / "data" / "GOLDEN-DATASETS.json").read_text())
    db.reset_db()
    ck = config.ROOT / "state" / "replay.db"
    ck.unlink(missing_ok=True)
    bad = 0
    for s in (x for x in data["sessions"] if x["id"] in ids):
        caller, group = s.get("caller_id") or CALLERS[s["id"]][0], s.get("caller_group") or CALLERS[s["id"]][1]
        for t in s["turns"]:
            r = handle_turn(caller, group, s["id"], t["user"], AS_OF, checkpoint_path=ck)
            fails = []
            if t.get("should_use_tools") is not None and bool(r.tool_sequence) != t["should_use_tools"]:
                fails.append(f"tool use {bool(r.tool_sequence)} != {t['should_use_tools']}")
            fails += [f"missing fact {f!r}" for f in t.get("required_facts") or [] if not has(r.answer, f)]
            fails += [f"forbidden fact {f!r}" for f in t.get("forbidden_facts") or [] if has(r.answer, f)]
            fails += [f"forbidden tool {x}" for x in r.tool_sequence if x in (t.get("forbidden_tools") or [])
                      or x in WRITE_TOOLS]
            fails += [f"forbidden source {x}" for x in r.sources if x in (t.get("forbidden_sources") or [])]
            bad += bool(fails)
            print(f"{s['id']} t{t['n']} {'FAIL' if fails else 'ok  '} intent={r.intent} status={r.status} "
                  f"tools={r.tool_sequence} {fails or ''}")
    get_client().flush()
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main(set(sys.argv[1:]) or {"S1-quick-policy"})
