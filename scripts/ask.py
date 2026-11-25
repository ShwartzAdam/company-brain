"""python scripts/ask.py --caller E010 [--group UG_REGULAR] [--thread T] "question"   (MCP server must be running)
Calls the same handle_turn the tests call. The clock is pinned to the evaluation date."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from langfuse import get_client  # noqa: E402

from brain.agent import handle_turn  # noqa: E402

AS_OF = "2026-07-01T12:00:00Z"

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--caller", required=True)
    ap.add_argument("--group", default="UG_REGULAR")
    ap.add_argument("--thread", default="cli")
    ap.add_argument("question")
    a = ap.parse_args()
    r = handle_turn(a.caller, a.group, a.thread, a.question, AS_OF)
    print(r.answer)
    print(f"\nstatus={r.status} scope={r.scope} intent={r.intent}\nvisible={r.visible_tools}\n"
          f"tools={r.tool_sequence}\nsources={r.sources}\nrequest_id={r.request_id}")
    get_client().flush()
