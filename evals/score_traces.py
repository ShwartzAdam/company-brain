"""Score a finished run FROM OUTSIDE, after the fact (lesson 11 ex.3): read each turn's trace back through the v2
API, run trace_checks, and push scores onto the trace. A better check can re-score old runs.

  python evals/score_traces.py evals/results/eval-current-sessions.json        # score + push
  python evals/score_traces.py evals/results/eval-current-sessions.json --dry  # score, push nothing
  (any results file written by session_replay.py or webex_replay.py; turns without a trace_id are skipped)
  python evals/score_traces.py --selftest

Traps handled (lesson 11): name every field group (no `io` = no tool arguments); the API lags writes, so an
unreadable trace is re-fetched and, if it stays unreadable, reported INVALID, never scored low; a check that does
not apply publishes no score. Reads Langfuse keys from .env; prints no secrets."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import trace_checks as tc  # noqa: E402

FIELDS = "core,basic,io,metadata,usage,model"


def fetch(lf, trace_id: str, tries: int = 6, wait: float = 10.0):
    """-> (observations, verdict, reasons). Re-fetches while the trace is INVALID (still arriving)."""
    obs, v, why = [], "INVALID", ["not fetched"]
    for i in range(tries):
        obs = lf.api.observations.get_many(trace_id=trace_id, fields=FIELDS, limit=200).data
        v, why = tc.verdict(obs)
        if v != "INVALID":
            break
        if i < tries - 1:
            time.sleep(wait)
    return obs, v, why


def score_run(lf, trace_id: str, ok: bool | None, scores: dict, push: bool = True, **fk) -> dict:
    """One turn. Pushes trace_followable (not when INVALID), turn_pass (when the replay judged it) and each diagnostic."""
    obs, v, why = fetch(lf, trace_id, **fk)
    out = {"trace_id": trace_id, "verdict": v, "reasons": why}
    pushes = {k: float(x) for k, x in (scores or {}).items()}
    if v != "INVALID":
        pushes["trace_followable"] = float(v == "PASS")
    if ok is not None:
        pushes["turn_pass"] = float(ok)
    if push:
        for k, x in pushes.items():
            lf.create_score(trace_id=trace_id, name=k, value=x, comment="; ".join(why)[:300] if k == "trace_followable" and why else None)
    out["pushed"] = pushes if push else {}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="?")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    from dotenv import load_dotenv
    from langfuse import get_client
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    lf = get_client()
    if not lf.auth_check():
        raise SystemExit("Langfuse rejected the keys: nothing would be recorded or read")
    res = json.loads(Path(a.results).read_text())
    rep, bad = [], 0
    for sid, turns in res.items():
        if not isinstance(turns, dict):
            continue
        for n, e in turns.items():
            for d in (e.get("detail") or []) if isinstance(e, dict) else []:
                if not d.get("trace_id"):
                    continue
                r = score_run(lf, d["trace_id"], d.get("ok"), d.get("scores"), push=not a.dry)
                rep.append((sid, n, r))
                bad += r["verdict"] == "FAIL"
                print(f"{sid} t{n} {r['verdict']:7} {d['trace_id'][:8]} {'; '.join(r['reasons'])[:140]}")
    lf.flush()
    print(f"{len(rep)} traces: {sum(r['verdict'] == 'PASS' for _, _, r in rep)} PASS, {bad} FAIL, "
          f"{sum(r['verdict'] == 'INVALID' for _, _, r in rep)} INVALID (unreadable, not scored)")
    sys.exit(1 if bad else 0)


def selftest():
    class Obs:  # one good observation set, no network
        data = [{"id": "r", "name": "agent.turn", "start_time": "1", "parent_observation_id": None, "type": "AGENT",
                 "output": "ok", "metadata": {k: "x" for k in tc.ROOT_KEYS} | {"status": "completed", "request_id": "q"}}]

    class Api:
        class observations:
            @staticmethod
            def get_many(**kw):
                assert "io" in kw["fields"] and "metadata" in kw["fields"]
                return Obs

    class LF:
        api, sent = Api, []

        def create_score(self, **kw):
            self.sent.append((kw["name"], kw["value"]))

    lf = LF()
    r = score_run(lf, "t1", True, {"intent_accuracy": 1.0}, tries=1)
    assert r["verdict"] != "INVALID" and ("turn_pass", 1.0) in lf.sent and ("intent_accuracy", 1.0) in lf.sent
    Obs.data = []
    lf.sent.clear()
    r = score_run(lf, "t2", None, {}, tries=1, wait=0)
    assert r["verdict"] == "INVALID" and not lf.sent  # unreadable: nothing published, never a zero
    print("score_traces selftest ok")


if __name__ == "__main__":
    main()
