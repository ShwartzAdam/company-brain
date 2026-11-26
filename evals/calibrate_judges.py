"""Live: run each judge on the hand-written cases and report whether it separates good from bad answers.
A judge that does not is not used as a gate. Costs about one model call per case.   python evals/calibrate_judges.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import judge  # noqa: E402


def run(cases: dict, fns=None) -> list:
    fns = fns or {"overstates": lambda c: judge.overstates(c["answer"], c["truth"])[:2],
                  "faithfulness": lambda c: judge.faithfulness(c["user"], c["evidence"], c["answer"])}
    bad = []
    for kind, lst in cases.items():
        if kind.startswith("_"):
            continue
        for c in lst:
            score, flagged = fns[kind](c)
            ok = (score < 0.7) == c["expect_low"]
            print(f"{'ok  ' if ok else 'WRONG'} {kind}/{c['id']} score={score:.2f} {flagged[:2]}")
            if not ok:
                bad.append(f"{kind}/{c['id']}")
    return bad


if __name__ == "__main__":
    bad = run(json.loads((Path(__file__).parent / "judge_calibration.json").read_text()))
    print("judges separate good from bad" if not bad else f"NOT trustworthy yet: {bad}")
    sys.exit(1 if bad else 0)
