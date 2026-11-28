"""The checks the golden dataset does not ship (its readme says they are ours to write):
  carry     a rule stated early still shows up in the late answer that needs it
  closed    a closed branch stops driving tool choice and stays out of later answers
  citations every tool-backed answer cites something, and never a source it did not retrieve
  writes    zero writes: no write tool called, and the database is byte-identical after the session
  handoff   exactly one typed handoff on S2 t10, none elsewhere, and replaying t10 files nothing more
  restart   the conversation survives the process dying (turns 1-6 in one process, turn 7 in another)

Contract Step 3 must satisfy (TurnResult.extra): {"handoff": dict|None, "handoff_calls": int}
  handoff keys: subject_employee_id, system, justification, conversation_id, caller_id, idempotency_key
  handoff_calls = how many times the Webex port was called in this conversation so far.

Live (Bedrock + MCP server). The check functions are pure, tested offline in tests/test_maya_checks.py.
  python evals/maya_checks.py [--label before]            exit 1 if any check fails
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from session_replay import AS_OF, RESULTS, has  # noqa: E402

from brain.graph.scope import WRITE_TOOLS  # noqa: E402

S2, S3, S4, S9 = "S2-onboarding-maya", "S3-intent-shift", "S4-constraint-carry", "S9-maya-own-account"
DATE = "2026-08-01|August 1|Aug 1|1 August"

# (session, turn, facts the answer must contain, must the turn use no tools, why)
CARRY = [
    (S2, 7, ["Israel", DATE], True, "turn-1 location and start date, recalled with zero tools"),
    (S2, 9, ["7,500|7500", "freeze|Q3", "Finance", "18,400|18400"], False, "turn-1 freeze + memo threshold + Webex cost"),
    (S2, 12, [DATE, "Israel", "Finance", "pending"], True, "summary: turn-1 facts, turn-9 Finance, turn-10 handoff"),
    (S4, 7, ["explicit|say so|until I|permission"], True, "turn-1 rule: do not file until told"),
    (S4, 8, ["Finance", "7,500|7500"], None, "turn-1 threshold still flagged when the user lifts the filing rule"),
    (S4, 9, ["Finance"], True, "wrap-up keeps the Finance rule"),
]
# (session, first turn, last turn, tools that must not run, words that must not appear in the answer)
CLOSED = [
    (S2, 5, 12, {"list_employee_tickets"}, []),
    (S2, 6, 12, set(), ["Rachel", "T001"]),
    (S3, 4, 4, {"check_software_subscription"}, []),
    (S3, 9, 9, set(), ["Rachel", "T001"]),
]
CITE_EXEMPT = {(S9, 4)}  # refusal turn: nothing the caller may cite exists
RECORD_ID = re.compile(r"\b(OT\d{3}|SUB\d{3}|C\d{3}|T\d{3}|A\d{3}|AR\d{3}|AP\d{3}|E\d{3})\b")
BRACKET = re.compile(r"\[([^\]\n]+)\]")
WRITE_SESSIONS = (S2, S9)


def _rec(recs, sid, n):
    return next((r for r in recs if r["session"] == sid and r["n"] == n), None)


def check_carry(recs) -> list:
    out = []
    for sid, n, facts, notools, why in CARRY:
        r = _rec(recs, sid, n)
        if r is None:
            out.append(f"{sid} t{n}: not run ({why})")
            continue
        out += [f"{sid} t{n}: missing {f!r} ({why})" for f in facts if not has(r["answer"], f)]
        if notools and r["tools"]:
            out.append(f"{sid} t{n}: used tools {r['tools']}, expected none")
    return out


def check_closed(recs) -> list:
    out = []
    for sid, a, b, tools, words in CLOSED:
        for n in range(a, b + 1):
            r = _rec(recs, sid, n)
            if r is None:
                out.append(f"{sid} t{n}: not run")
                continue
            out += [f"{sid} t{n}: closed-branch tool {t}" for t in r["tools"] if t in tools]
            out += [f"{sid} t{n}: closed-branch word {w!r} in answer" for w in words if has(r["answer"], w)]
    r8 = _rec(recs, S3, 8)
    if r8 and (r8["tools"] or not has(r8["answer"], "Rachel")):
        out.append(f"{S3} t8: durable fact lost or tools used (tools={r8['tools']})")
    return out


def check_citations(recs) -> list:
    out, seen = [], {}
    for r in recs:
        known = seen.setdefault(r["session"], set())
        known.update(r["sources"])
        if not r["tools"] or (r["session"], r["n"]) in CITE_EXEMPT:
            continue
        cited = [c.strip() for c in BRACKET.findall(r["answer"]) if "/" in c or c.strip().endswith(".md")]
        if not cited and not RECORD_ID.search(r["answer"]):
            out.append(f"{r['session']} t{r['n']}: tool-backed answer cites nothing")
        out += [f"{r['session']} t{r['n']}: cites {c!r} which was never retrieved"
                for c in cited if not any(s == c or s.endswith("/" + c) or c.endswith(s) for s in known)]
    return out


def check_faithful(recs, judge, threshold=0.8) -> list:
    """judge(user_msgs, evidence, answer) -> (score, unsupported). Session-level evidence: everything gathered so far."""
    out, ev, msgs, cur = [], {}, {}, None
    for r in recs:
        ev.setdefault(r["session"], []).extend(r.get("evidence") or [])
        msgs.setdefault(r["session"], []).append(r.get("user", ""))
        score, bad = judge(msgs[r["session"]], ev[r["session"]], r["answer"])
        if score < threshold:
            out.append(f"{r['session']} t{r['n']}: faithfulness {score:.2f}, unsupported {bad[:3]}")
    return out


def check_args_all(recs, golden) -> list:
    """Argument correctness for every replayed turn (same scorer as the replay)."""
    import session_replay as sr
    out = []
    for r in recs:
        t = golden.get(r["session"], {}).get(r["n"])
        out += [f"{r['session']} t{r['n']}: {f}" for f in sr.check_args(t or {}, r.get("calls") or [])]
    return out


def check_writes(recs, before: dict, after: dict) -> list:
    out = [f"{r['session']} t{r['n']}: write tool {t}" for r in recs for t in r["tools"] if t in WRITE_TOOLS]
    for sid, b in before.items():
        out += [f"{sid}: table {t} changed" for t in b if b[t] != after.get(sid, {}).get(t)]
    return out


def check_handoff(recs, replay) -> list:
    out = []
    for r in (x for x in recs if x["session"] == S2):
        h = (r.get("extra") or {}).get("handoff")
        if r["n"] != 10 and h:
            out.append(f"{S2} t{r['n']}: handoff emitted on a turn that did not ask to act")
    r10 = _rec(recs, S2, 10)
    h = ((r10 or {}).get("extra") or {}).get("handoff")
    if not h:
        return out + [f"{S2} t10: no typed handoff (extra.handoff missing)"]
    if h.get("subject_employee_id") != "E001":
        out.append(f"{S2} t10: subject {h.get('subject_employee_id')!r}, expected E001 (not Maya Rosen E020)")
    if "webex" not in str(h.get("system", "")).lower():
        out.append(f"{S2} t10: system {h.get('system')!r}, expected Webex")
    out += [f"{S2} t10: handoff missing {k}" for k in ("justification", "conversation_id", "caller_id", "idempotency_key")
            if not h.get(k)]
    if h.get("caller_id") != "E004":
        out.append(f"{S2} t10: caller {h.get('caller_id')!r}, expected E004 (argument, not message text)")
    if (r10["extra"].get("handoff_calls") or 0) != 1:
        out.append(f"{S2} t10: port called {r10['extra'].get('handoff_calls')} times, expected 1")
    if replay is None or (replay.get("extra") or {}).get("handoff_calls") != 1:
        out.append(f"{S2} replay of t10: port calls {((replay or {}).get('extra') or {}).get('handoff_calls')}, expected still 1")
    return out


def check_restart(rec7) -> list:
    if rec7 is None:
        return ["restart: turn 7 in a fresh process did not return"]
    out = [f"restart: missing {f!r} after process restart" for f in ("Israel", DATE) if not has(rec7["answer"], f)]
    return out + ([f"restart: used tools {rec7['tools']}"] if rec7["tools"] else [])


# ---------------------------- live runner ----------------------------

TABLES = ("tickets", "access_requests", "approvals", "write_keys", "audit_log", "onboarding_tasks", "assets",
          "software_subscriptions")


def snapshot() -> dict:
    from server import db
    return {t: hashlib.sha256(repr(db.rows(f"SELECT * FROM {t} ORDER BY 1")).encode()).hexdigest()[:12] for t in TABLES}


def run_turns(session, first, last, thread, ck) -> list:
    from brain.agent import handle_turn
    recs = []
    for t in session["turns"]:
        if first <= t["n"] <= last:
            r = handle_turn(session["caller_id"], session["caller_group"], thread, t["user"], AS_OF, checkpoint_path=ck)
            recs.append({"session": session["id"], "n": t["n"], "answer": r.answer, "tools": r.tool_sequence,
                         "sources": r.sources, "status": r.status, "user": t["user"],
                         "calls": r.extra.get("tool_calls", []), "evidence": r.extra.get("evidence", []),
                         "extra": {k: v for k, v in r.extra.items() if k not in ("evidence", "tool_calls")}})
            print(f"  {session['id']} t{t['n']} tools={r.tool_sequence} status={r.status}")
    return recs


def _golden(sid):
    from brain import config
    d = json.loads((config.ROOT / "data" / "GOLDEN-DATASETS.json").read_text())
    return next(s for s in d["sessions"] if s["id"] == sid)


def _session_with_caller(sid):
    s = _golden(sid)
    s.setdefault("caller_id", {S3: "E007", S4: "E006"}.get(sid))
    s.setdefault("caller_group", {S3: "UG_IT", S4: "UG_IT"}.get(sid))
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="maya_checks")
    ap.add_argument("--no-judge", action="store_true", help="skip the model-judged faithfulness check")
    ap.add_argument("--child", nargs=5, metavar=("SESSION", "FIRST", "LAST", "THREAD", "CK"))
    a = ap.parse_args()
    if a.child:  # a fresh process for the restart check: print the records as one JSON line
        sid, first, last, thread, ck = a.child
        print("REC " + json.dumps(run_turns(_session_with_caller(sid), int(first), int(last), thread, Path(ck))))
        return

    from brain import config

    from server import db
    ck = config.ROOT / "state" / "maya_checks.db"
    recs, before, after = [], {}, {}
    replay = None
    for sid in (S2, S9, S3, S4):
        db.reset_db()
        ck.unlink(missing_ok=True)
        s = _session_with_caller(sid)
        b = snapshot()
        got = run_turns(s, 1, 99, sid, ck)
        if sid == S2:  # replay turn 10 on the same thread: must not file a second handoff
            replay = run_turns({**s, "turns": [t for t in s["turns"] if t["n"] == 10]}, 10, 10, sid, ck)[0]
        recs += got
        if sid in WRITE_SESSIONS:
            before[sid], after[sid] = b, snapshot()
    db.reset_db()
    ck.unlink(missing_ok=True)
    s2 = _session_with_caller(S2)
    py, me = sys.executable, str(Path(__file__))
    subprocess.run([py, me, "--child", S2, "1", "6", "S2-restart", str(ck)], check=True, capture_output=True)
    p = subprocess.run([py, me, "--child", S2, "7", "7", "S2-restart", str(ck)], capture_output=True, text=True)
    line = next((x for x in p.stdout.splitlines() if x.startswith("REC ")), None)
    rec7 = json.loads(line[4:])[0] if line else None

    results = {"carry": check_carry(recs), "closed": check_closed(recs), "citations": check_citations(recs),
               "writes": check_writes([r for r in recs if r["session"] in WRITE_SESSIONS], before, after),
               "handoff": check_handoff(recs, replay), "restart": check_restart(rec7),
               "arguments": check_args_all(recs, {sid: {t["n"]: t for t in _session_with_caller(sid)["turns"]}
                                                  for sid in (S2, S3, S4, S9)})}
    if not a.no_judge:
        import judge
        results["faithful"] = check_faithful([r for r in recs if r["session"] in (S2, S9)], judge.faithfulness)
    for name, fails in results.items():
        print(f"{'PASS' if not fails else 'FAIL'} {name}" + "".join(f"\n    - {f}" for f in fails))
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{a.label}.json").write_text(json.dumps(results, indent=1))
    sys.exit(1 if any(results.values()) else 0)


if __name__ == "__main__":
    main()
