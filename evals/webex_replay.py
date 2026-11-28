"""Live Webex baseline (Bedrock + OpenSearch + our own MCP server). Starts its own server on a private port and
database so it never touches another session's state.

  python evals/webex_replay.py --runs 3 --label step4          # W-I-01..03, then S4, S6, S7, S8
  python evals/webex_replay.py --runs 1 --cases W-I-03 --sessions   # a quick look

Per case and run: reseed the database, run the case, check the binding fields (session_replay.check_turn), the
lesson 11 diagnostics (intent accuracy, tool necessity), and the Webex checks (rows, replay, never-granted,
pause). W-I-03 then replays itself (same thread, new thread), approves the recorded rows and resumes once.
Saves evals/results/<label>.json (no answers) and prints a baseline table."""
import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("BRAIN_DB_PATH", str(ROOT / "state" / "step4.db"))
os.environ.setdefault("MCP_PORT", "9891")
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))
import checks  # noqa: E402
import session_replay as sr  # noqa: E402
import webex_checks as wc  # noqa: E402
import judge  # noqa: E402

from brain import agent, config  # noqa: E402

from brain.webex import gate

from server import db

JUDGE = {"on": True}  # --no-judge turns the model-judged scores off (they cost a call each)
TABLES = ("tickets", "access_requests", "approvals", "audit_log", "write_keys", "consents")
SESSIONS = ["S4-constraint-carry", "S6-boundary-clean", "S7-boundary-noisy", "S8-boundary-heavy-noise"]


def snap() -> dict:
    return {t: len(db.rows(f"SELECT 1 FROM {t}")) for t in TABLES}


def approvals() -> dict:
    return {r["approval_id"]: r["status"] for r in db.rows("SELECT approval_id, status FROM approvals WHERE request_id='AR001'")}


def start_server():
    p = subprocess.Popen([sys.executable, "-m", "server.server"], cwd=ROOT, env=os.environ.copy(),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            socket.create_connection((config.MCP_HOST, config.MCP_PORT), 0.5).close()
            return p
        except OSError:
            time.sleep(0.5)
    p.kill()
    raise SystemExit("MCP server did not start")


def run_case(c: dict, run: int, ck: Path) -> dict:
    db.reset_db()
    ck.unlink(missing_ok=True)
    before, tid = snap(), f"{c['id']}#{run}"
    r = agent.handle_turn(c["caller"], c["group"], tid, c["message"], sr.AS_OF, checkpoint_path=ck, case_id=c["id"], turn=1)
    calls = r.extra.get("tool_calls") or []
    fails = sr.check_turn(c, r.answer, r.tool_sequence, r.sources, calls)
    sc = checks.score_turn(c, calls, r.intent)
    dec = r.extra.get("decision")
    state = c.get("state") or {}
    allow = state.get("extra_rows", {})
    after = snap()
    fails += wc.check_rows(before, after, allow)
    fails += wc.check_state(approvals(), state.get("approvals", {})) if state else []
    fails += wc.check_never_granted(r.answer, dec, db.rows("SELECT status FROM access_requests WHERE request_id='AR001'")[0]["status"], approvals())
    fails += wc.check_status_agrees(r.status, r.answer)
    out = {"ok": not fails, "fails": fails, "intent": r.intent, "status": r.status, "tools": r.tool_sequence,
           "scores": {k: v[0] for k, v in sc.items()}, "sources": r.sources, "answer_head": r.answer[:160], "error": r.error, "trace_id": r.trace_id}
    if JUDGE["on"]:  # judged scores are diagnostics (uncalibrated until calibrate_judges passes), never part of `ok`
        rec = {"tools": r.tool_sequence, "status": r.status, "extra": r.extra}
        if "faithfulness" in judge.judges_for(rec):
            out["scores"]["judge_faithfulness"] = judge.faithfulness([c["message"]], r.extra.get("evidence") or [], r.answer)[0]
        if "overstates" in judge.judges_for(rec):
            out["scores"]["judge_overstates"] = judge.overstates(r.answer, judge.truth_of(rec))[0]
    if c["id"] == "W-I-03":
        out["paused"] = bool(r.extra.get("paused"))
        first = snap()
        agent.handle_turn(c["caller"], c["group"], tid, c["message"], sr.AS_OF, checkpoint_path=ck)       # replay, same thread
        out["fails"] += wc.check_replay(first, snap())              # nothing at all changes, not even the write ledger
        agent.handle_turn(c["caller"], c["group"], tid + "-new", c["message"], sr.AS_OF, checkpoint_path=ck)  # replay, new thread
        now = snap()
        LEDGERS = ("write_keys", "consents")  # a new conversation adds ledger rows, never business rows
        out["fails"] += wc.check_replay({k: v for k, v in first.items() if k not in LEDGERS}, {k: v for k, v in now.items() if k not in LEDGERS})
        first = now
        if not out["paused"]:
            out["fails"].append("did not pause")
        waiting = agent.resume_pending(c["caller"], c["group"], tid, sr.AS_OF, checkpoint_path=ck)       # rows not approved
        if waiting.status != "pending" or snap() != first:
            out["fails"].append(f"resume before approval changed state or status ({waiting.status})")
        cx = db.conn()
        cx.execute("UPDATE approvals SET status='approved' WHERE request_id='AR001'")
        cx.commit()
        cx.close()
        done = agent.resume_pending(c["caller"], c["group"], tid, sr.AS_OF, checkpoint_path=ck)
        agent.resume_pending(c["caller"], c["group"], tid, sr.AS_OF, checkpoint_path=ck)                  # a second resume is a no-op
        rel = len(db.rows("SELECT 1 FROM audit_log WHERE event_type='access_request_released'"))
        if done.status != "needs_human" or rel != 1:
            out["fails"].append(f"resume after approval: status {done.status}, release events {rel}")
        out["fails"] += wc.check_never_granted(done.answer, None, "approved", approvals())
        out["fails"] += wc.check_write_needs_consent([r["key"] for r in db.rows("SELECT key FROM write_keys")], consent_rows())
        out["ok"] = not out["fails"]
    print(f"{tid} {'FAIL' if out['fails'] else 'ok  '} intent={r.intent} status={r.status} tools={r.tool_sequence} {out['fails'] or ''} {r.error}")
    return out


def consent_rows() -> list:
    return db.rows("SELECT message_id, caller_id, employee_id, system, consumed FROM consents ORDER BY message_id")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--label", default="step4")
    ap.add_argument("--cases", nargs="*")
    ap.add_argument("--sessions", action="store_true", help="only the sessions (skip single cases)")
    ap.add_argument("--only-cases", action="store_true")
    ap.add_argument("--session-ids", nargs="*", help="subset of the golden sessions to replay (default S4, S6, S7, S8)")
    ap.add_argument("--baseline")
    ap.add_argument("--no-judge", action="store_true")
    a = ap.parse_args()
    JUDGE["on"] = not a.no_judge
    from langfuse import get_client
    server = start_server()
    res: dict = {}
    try:
        cases = json.loads((Path(__file__).resolve().parent / "webex_cases.json").read_text())["cases"]
        ck = ROOT / "state" / "step4_replay.db"
        if not a.sessions:
            for c in cases:
                if a.cases and c["id"] not in a.cases:
                    continue
                e = res.setdefault(c["id"], {"1": {"runs": 0, "passes": 0, "fails": [], "scores": {}}})["1"]
                for run in range(1, a.runs + 1):
                    r = run_case(c, run, ck)
                    e["runs"] += 1
                    e["passes"] += r["ok"]
                    e.setdefault("detail", []).append({"trace_id": r["trace_id"], "ok": r["ok"], "scores": r["scores"]})
                    e["fails"] = sorted(set(e["fails"]) | set(r["fails"]))
                    e["status"], e["tools"], e["sources"] = r["status"], r["tools"], r["sources"]
                    for k, v in r["scores"].items():
                        m = e["scores"].setdefault(k, [0.0, 0])
                        m[0] += v
                        m[1] += 1
                e["scores"] = {k: round(v[0] / v[1], 2) for k, v in e["scores"].items()}
        if not a.only_cases:
            gold = {s["id"]: s for s in sr.json.loads((config.ROOT / "data" / "GOLDEN-DATASETS.json").read_text())["sessions"]}
            for sid in a.session_ids or SESSIONS:
                agg: dict = {}
                for run in range(1, a.runs + 1):
                    db.reset_db()
                    ck.unlink(missing_ok=True)
                    before = snap()
                    go = next((t["n"] for t in gold[sid]["turns"] if gate.consent_from_message(t["user"])), None)
                    hook = lambda t, r, b=before, g=go: wc.check_nofile_carry(b, snap(), t["n"], g)  # noqa: E731
                    for n, r in sr.run_session(gold[sid], run, ck, agent.handle_turn, after_turn=hook).items():
                        e = agg.setdefault(n, {"runs": 0, "passes": 0, "fails": [], "tools": r["tools"], "status": r["status"]})
                        e["runs"] += 1
                        e["passes"] += r["ok"]
                        e.setdefault("detail", []).append({"trace_id": r.get("trace_id", ""), "ok": r["ok"], "scores": r.get("scores") or {}})
                        e["fails"] = sorted(set(e["fails"]) | set(r["fails"]))
                    print(f"  {sid}#{run}: rows {before} -> {snap()}; consent rows {consent_rows()}")
                res[sid] = agg
    finally:
        server.kill()
    get_client().flush()
    sr.RESULTS.mkdir(exist_ok=True)
    (sr.RESULTS / f"{a.label}.json").write_text(json.dumps(res, indent=1, sort_keys=True))
    total = sum(v["runs"] for s in res.values() for v in s.values())
    passed = sum(v["passes"] for s in res.values() for v in s.values())
    print(f"\n{passed}/{total} turn-runs passed -> evals/results/{a.label}.json")
    for sid, turns in res.items():
        bad = [f"t{n} {v['passes']}/{v['runs']}" for n, v in turns.items() if v["passes"] < v["runs"]]
        print(f"  {sid}: {'all pass' if not bad else 'NOT all pass: ' + ', '.join(bad)}")
    if a.baseline:
        reg = sr.compare(json.loads(Path(a.baseline).read_text()), res)
        print("REGRESSIONS:" if reg else "no regressions vs baseline", *reg, sep="\n  ")
        sys.exit(1 if reg else 0)


if __name__ == "__main__":
    main()
