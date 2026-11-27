"""Run EVALUATION-INPUTS.yaml and produce the trace index (spec 3.2, M10). Required set: Maya 20 + Webex 7 = 27 traces.

  python evals/run_submission.py --dry-run                  # no AWS, no Langfuse: proves the plan, threads, groups, table
  python evals/run_submission.py --rehearsal                # LIVE (Bedrock + Langfuse), environment=rehearsal
  python evals/run_submission.py --submit --fill SUBMISSION.md   # LIVE, environment=submission, writes trace ids

Inputs are one-shot (new thread each); sessions replay every turn IN ORDER on one thread. The database is reseeded
before each item so one item's writes never change another's. as_of is pinned (spec 13). The run only READS
the yaml; expectations live in our own evals, never here (the yaml is inputs only, by design)."""
import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

AS_OF = "2026-07-01T12:00:00Z"
GROUPS = {"Regular Employee": "UG_REGULAR", "HR Group": "UG_HR", "IT Group": "UG_IT"}
REQUIRED = ("maya", "webex")
EXPECTED = {"maya": 20, "webex": 7}
YAML = ROOT / "data" / "EVALUATION-INPUTS.yaml"


def load(path=YAML, optional: bool = False) -> list[dict]:
    """-> flat list of turns: {item, workflow, turn, thread, caller, group, message}. `turn` is None for a one-shot input."""
    import yaml
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    out = []
    for w in doc["workflows"]:
        if w["id"] not in REQUIRED and not (optional and not w.get("required")):
            continue
        for it in w.get("inputs") or []:
            if "caller" not in it:
                continue  # vendor inputs have no caller: not in the required set
            out.append(_turn(w["id"], it["id"], None, it["caller"], it["message"]))
        for s in w.get("sessions") or []:
            out += [_turn(w["id"], s["id"], t["n"], s["caller"], t["message"]) for t in s["turns"]]
    return out


def _turn(workflow, item, n, caller, message):
    return {"item": item, "workflow": workflow, "turn": n, "thread": item, "caller": caller["employee_id"],
            "group": GROUPS[caller["group"]], "message": message}


def counts(turns: list[dict]) -> dict:
    return {w: sum(t["workflow"] == w for t in turns) for w in {t["workflow"] for t in turns}}


def fill(md: str, ids: dict) -> str:
    """Put trace ids into SUBMISSION.md rows `| `M-S-01` | 3 | |`; nothing else is touched. ids: {(item, turn|None): id}."""
    def row(m):
        key = (m.group(1), None if m.group(2) == "—" else int(m.group(2)))
        return f"| `{m.group(1)}` | {m.group(2)} | {ids[key]} |" if ids.get(key) else m.group(0)
    return re.sub(r"^\| `([A-Z]-[IS]-\d\d)` \| (—|\d+) \|[ ]*\|$", row, md, flags=re.M)


async def run_all(turns, handle, reset, prefix="", environment=None, ck=None, save=None, timeout=None):
    """Sequential, in order. -> rows [{item, turn, thread, trace_id, status, request_id}]. A turn that raises or exceeds
    `timeout` becomes a `failed` row and the run goes on; `save(rows)` runs after EVERY turn so a crash keeps what finished."""
    from brain import config
    timeout = timeout or config.RUN_TURN_TIMEOUT_S
    rows, last = [], None
    for t in turns:
        if t["item"] != last:  # a new item: independent database and conversation
            reset()
            if ck:
                Path(ck).unlink(missing_ok=True)
            last = t["item"]
        try:
            r = await asyncio.wait_for(handle(t["caller"], t["group"], prefix + t["thread"], t["message"], AS_OF, case_id=t["item"],
                                              turn=t["turn"] if t["turn"] is not None else 1, environment=environment), timeout)
            row = {"item": t["item"], "turn": t["turn"], "thread": prefix + t["thread"], "trace_id": r.trace_id,
                   "status": r.status, "request_id": r.request_id, "intent": r.intent}
        except Exception as e:  # noqa: BLE001  (includes the timeout)
            why = "timeout" if isinstance(e, asyncio.TimeoutError) else f"{type(e).__name__}: {e}"[:200]
            row = {"item": t["item"], "turn": t["turn"], "thread": prefix + t["thread"], "trace_id": "", "status": "failed",
                   "request_id": "", "intent": "", "error": why}
        rows.append(row)
        if save:
            save(rows)
        print(f"{t['item']} t{t['turn'] or '-'} {row['status']:11} {row['intent']:18} trace={row['trace_id'][:8] or '(none)'}"
              + (f" ERROR {row['error']}" if row.get("error") else ""))
    return rows


class DryBackend:
    """Answers every call shape with something valid, so the plumbing runs with no AWS. The dry run proves threads, groups,
    ids and the table fill; it does NOT exercise real evidence gathering, the handoff or the pause (classify says
    `other` with no tools), so a green dry run says nothing about answer quality."""
    def complete(self, messages, system, max_tokens, temperature):
        if "classify the newest message" in system.lower():
            return json.dumps({"intent": "other", "requires_tools": False})
        if "gather evidence" in system.lower():
            return '{"done": true}'
        return "Dry run answer: no model was called."

    def embed(self, text):
        return [0.0] * 1024


def dry_run(turns, save=None) -> list[dict]:
    """The whole path with a fake model, tracing off, in-memory MCP. Trace ids are synthetic: dry-<request id>."""
    os.environ["LANGFUSE_TRACING_ENABLED"] = "false"
    os.environ["BRAIN_DB_PATH"] = tempfile.mkdtemp() + "/dry.db"
    from mcp.shared.memory import create_connected_server_and_client_session as connect
    from brain import agent, db, llm, mcp_client
    from server.server import mcp
    llm.set_backend(DryBackend())
    ck = Path(tempfile.mkdtemp()) / "ck.db"

    async def go():
        async with connect(mcp._mcp_server) as s:
            boxes = {}

            async def handle(caller, group, thread, msg, as_of, **kw):
                box = boxes.get(caller) or boxes.setdefault(caller, await mcp_client.Toolbox(s, caller).load())
                r = await agent.ahandle_turn(caller, group, thread, msg, as_of, toolbox=box, checkpoint_path=ck, **kw)
                r.trace_id = r.trace_id or f"dry-{r.request_id}"
                return r
            return await run_all(turns, handle, db.reset_db, ck=ck, save=save)
    try:
        return asyncio.run(go())
    finally:
        llm.set_backend(None)


def live(turns, environment: str, prefix: str, save=None):
    os.environ["LANGFUSE_TRACING_ENVIRONMENT"] = environment  # before the Langfuse client is created
    import webex_replay as wr  # sets MCP_PORT / BRAIN_DB_PATH for a private server and database
    from dotenv import load_dotenv
    from langfuse import get_client
    load_dotenv(ROOT / ".env")
    lf = get_client()
    if not lf.auth_check():  # wrong or missing keys silently turn tracing into a no-op: stop before spending anything
        raise SystemExit("Langfuse rejected the keys: a run now would record nothing")
    from brain import agent, db
    server = wr.start_server()
    ck = ROOT / "state" / f"submission_{environment}.db"
    try:
        async def handle(*a, **kw):
            return await agent.ahandle_turn(*a, checkpoint_path=ck, **kw)
        rows = asyncio.run(run_all(turns, handle, db.reset_db, prefix=prefix, environment=environment, ck=ck, save=save))
    finally:
        server.terminate()
    lf.flush()
    return rows


def verify(rows) -> list[str]:
    """Every id must resolve through the v2 API and be followable. INVALID (still arriving) is reported, not hidden."""
    import score_traces as st
    from dotenv import load_dotenv
    from langfuse import get_client
    load_dotenv(ROOT / ".env")
    lf, bad = get_client(), []
    for r in rows:
        if not r["trace_id"]:  # a failed turn with no trace: report it, do not look it up
            print(f"{r['item']} t{r['turn'] or '-'} NO TRACE ({r.get('error', '')})")
            bad.append(f"{r['item']} t{r['turn']}: no trace ({r.get('error', 'failed')})")
            continue
        _, v, why = st.fetch(lf, r["trace_id"])
        print(f"{r['item']} t{r['turn'] or '-'} {v} {'; '.join(why)[:120]}")
        if v != "PASS":
            bad.append(f"{r['item']} t{r['turn']}: {v}")
    return bad


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--rehearsal", action="store_true")
    g.add_argument("--submit", action="store_true")
    ap.add_argument("--yaml", default=str(YAML))
    ap.add_argument("--fill", help="SUBMISSION.md to write trace ids into (only with --submit)")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--items", nargs="*", help="only these item ids (a partial run: never counted as the 27, never written to SUBMISSION.md)")
    a = ap.parse_args()
    turns = load(a.yaml)
    c = counts(turns)
    assert c == EXPECTED, f"expected {EXPECTED}, the yaml has {c}"
    if a.items:
        turns, a.fill = [t for t in turns if t["item"] in a.items], None
    print(f"{len(turns)} traces planned: {c}")
    env = "submission" if a.submit else "rehearsal"
    out = ROOT / "evals" / "results" / f"submission-{'dry' if a.dry_run else env}{'-partial' if a.items else ''}.json"
    out.parent.mkdir(exist_ok=True)
    save = lambda rows: out.write_text(json.dumps(rows, indent=1))  # noqa: E731  after every turn
    rows = dry_run(turns, save) if a.dry_run else live(turns, env, "" if a.submit else "rehearsal-", save)
    save(rows)
    ids = {(r["item"], r["turn"]): r["trace_id"] for r in rows}
    if a.dry_run:
        print(f"dry run ok: {len(rows)} turns, {len({r['thread'] for r in rows})} threads -> {out.name}")
        return
    bad = [] if a.no_verify else verify(rows)
    if a.fill and a.submit:
        p = Path(a.fill)
        p.write_text(fill(p.read_text(), ids))
        print(f"trace ids written to {p}")
    print("ALL FOLLOWABLE" if not bad else "NOT ALL FOLLOWABLE:\n  " + "\n  ".join(bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
