"""Replay golden sessions turn by turn on one thread and check the BINDING fields only
(required_facts, forbidden_facts, should_use_tools, forbidden_tools, forbidden_sources, expected_sources).
Live Bedrock + MCP server. Writes evals/results/<label>.json (no answers, no secrets) and can compare it
with an earlier file; a turn that passed before and fails now is a regression (exit 1).

  python evals/session_replay.py                         # all sessions, 1 run
  python evals/session_replay.py S1-quick-policy S3-intent-shift --runs 3 --label step2
  python evals/session_replay.py --label step3 --baseline evals/results/step2.json
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import checks  # noqa: E402
from brain import config  # noqa: E402
from brain.scope import WRITE_TOOLS  # noqa: E402

AS_OF = "2026-07-01T12:00:00Z"
RESULTS = Path(__file__).resolve().parent / "results"
CALLERS = {"S1-quick-policy": ("E010", "UG_REGULAR"), "S3-intent-shift": ("E007", "UG_IT"),
           "S4-constraint-carry": ("E006", "UG_IT"), "S6-boundary-clean": ("E004", "UG_HR"),
           "S7-boundary-noisy": ("E004", "UG_HR"), "S8-boundary-heavy-noise": ("E004", "UG_HR")}


def has(text: str, spec: str) -> bool:
    return any(alt.strip().lower() in text.lower() for alt in spec.split("|"))


def src_match(path: str, spec: str) -> bool:
    """Golden specs are 'manager_playbook/x.md' or 'x.md'; retrieved paths are 'documents/manager_playbook/x.md'."""
    return path == spec or path.endswith("/" + spec)


def check_args(t: dict, calls: list) -> list:
    """Argument correctness for tools we share with the reference (same names and arg names).
    `equals` (an id): EVERY call to that tool must use it, so a lookup for the wrong Maya is a failure.
    `contains` (a name): at least one call must. A tool that was not called is a selection question, not this one."""
    fails = []
    for c in t.get("arg_checks") or []:
        mine = [x["args"] for x in calls if x["tool"] == c["tool"] and c["arg"] in (x.get("args") or {})]
        if not mine:
            continue
        if "equals" in c:
            fails += [f"{c['tool']}.{c['arg']}={a[c['arg']]!r}, expected {c['equals']!r}" for a in mine
                      if a[c["arg"]] != c["equals"]]
        elif "contains" in c and not any(c["contains"].lower() in str(a[c["arg"]]).lower() for a in mine):
            fails.append(f"{c['tool']}.{c['arg']} never contains {c['contains']!r}")
    return fails


def check_turn(t: dict, answer: str, tools: list, sources: list, calls: list | None = None) -> list:
    """Binding fields only. Returns a list of failure strings (empty = pass)."""
    fails = check_args(t, calls or [])
    if t.get("should_use_tools") is not None and bool(tools) != t["should_use_tools"]:
        fails.append(f"tool use {bool(tools)} != {t['should_use_tools']}")
    fails += [f"missing fact {f!r}" for f in t.get("required_facts") or [] if not has(answer, f)]
    fails += [f"forbidden fact {f!r}" for f in t.get("forbidden_facts") or [] if has(answer, f)]
    fails += [f"forbidden tool {x}" for x in tools if x in (t.get("forbidden_tools") or []) or x in WRITE_TOOLS]
    fails += [f"forbidden source {s}" for s in sources
              if any(src_match(s, f) for f in t.get("forbidden_sources") or [])]
    fails += [f"expected source missing {e}" for e in t.get("expected_sources") or []
              if not any(src_match(s, e) for s in sources)]
    return fails


def compare(base: dict, new: dict) -> list:
    """Regressions: a turn whose pass rate dropped versus the baseline. Turns missing from either side are ignored."""
    out = []
    for sid, turns in new.items():
        for n, cur in turns.items():
            old = (base.get(sid) or {}).get(n)
            if old and cur["passes"] / cur["runs"] < old["passes"] / old["runs"]:
                out.append(f"{sid} t{n}: {old['passes']}/{old['runs']} -> {cur['passes']}/{cur['runs']} {cur['fails'][:2]}")
    return out


def run_session(s: dict, run: int, ck: Path, handle_turn, after_turn=None) -> dict:
    """after_turn(turn, result) -> list of extra failure strings (per-turn checks that need state outside the answer)."""
    caller = s.get("caller_id") or CALLERS[s["id"]][0]
    group = s.get("caller_group") or CALLERS[s["id"]][1]
    out = {}
    for t in s["turns"]:
        r = handle_turn(caller, group, f"{s['id']}#{run}", t["user"], AS_OF, checkpoint_path=ck,
                        case_id=s["id"], turn=t["n"])
        fails = check_turn(t, r.answer, r.tool_sequence, r.sources, r.extra.get("tool_calls"))
        fails += (after_turn(t, r) if after_turn else [])
        sc = checks.score_turn(t, r.extra.get("tool_calls") or [], r.intent)  # lesson 11 diagnostics, not part of ok
        out[str(t["n"])] = {"ok": not fails, "fails": fails, "tools": r.tool_sequence, "status": r.status,
                            "intent": r.intent, "scores": {k: v[0] for k, v in sc.items()}, "trace_id": r.trace_id}
        print(f"{s['id']}#{run} t{t['n']} {'FAIL' if fails else 'ok  '} intent={r.intent} status={r.status} "
              f"tools={r.tool_sequence} {fails or ''}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="*")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--label", default="latest")
    ap.add_argument("--baseline")
    a = ap.parse_args()

    from langfuse import get_client
    from brain import db
    from brain.agent import handle_turn

    data = json.loads((config.ROOT / "data" / "GOLDEN-DATASETS.json").read_text())
    ck = config.ROOT / "state" / "replay.db"
    res: dict = {}
    for s in data["sessions"]:
        if a.sessions and s["id"] not in a.sessions:
            continue
        if not (s.get("caller_id") or s["id"] in CALLERS):
            print(f"skip {s['id']}: no caller (add to CALLERS)")
            continue
        agg: dict = {}
        for run in range(1, a.runs + 1):
            db.reset_db()  # each run is an independent case on a fresh database
            ck.unlink(missing_ok=True)
            for n, r in run_session(s, run, ck, handle_turn).items():
                e = agg.setdefault(n, {"runs": 0, "passes": 0, "fails": [], "tools": r["tools"], "status": r["status"]})
                e["runs"] += 1
                e["passes"] += r["ok"]
                e.setdefault("detail", []).append({"trace_id": r.get("trace_id", ""), "ok": r["ok"], "scores": r.get("scores") or {}})
                for k, v in (r.get("scores") or {}).items():  # mean per diagnostic across runs
                    m = e.setdefault("scores", {}).setdefault(k, [0.0, 0])
                    m[0] += v
                    m[1] += 1
                e["fails"] = sorted(set(e["fails"]) | set(r["fails"]))
        res[s["id"]] = agg
    get_client().flush()
    for sess in res.values():
        for e in sess.values():
            e["scores"] = {k: round(v[0] / v[1], 2) for k, v in (e.get("scores") or {}).items()}
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{a.label}.json").write_text(json.dumps(res, indent=1, sort_keys=True))
    total = sum(v["runs"] for s in res.values() for v in s.values())
    passed = sum(v["passes"] for s in res.values() for v in s.values())
    print(f"\n{passed}/{total} turn-runs passed -> evals/results/{a.label}.json")
    for sid, turns in res.items():
        flaky = [f"t{n} {v['passes']}/{v['runs']}" for n, v in turns.items() if 0 < v["passes"] < v["runs"]]
        bad = [f"t{n}" for n, v in turns.items() if v["passes"] == 0]
        print(f"  {sid}: always-fail {bad or '-'}  flaky {flaky or '-'}")
    regress = []
    if a.baseline:
        regress = compare(json.loads(Path(a.baseline).read_text()), res)
        print("REGRESSIONS:" if regress else "no regressions vs baseline", *regress, sep="\n  ")
    sys.exit(1 if regress else 0)


if __name__ == "__main__":
    main()
