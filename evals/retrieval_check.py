"""Labeled retrieval check (live). recall@5 of expected DOCUMENTS (database tables are not
retrievable here, so they are not scored). Cases that need the agent's second search are
listed, not scored. Retrieval is not deterministic (approximate kNN + model rerank), so
use --runs N and read the mean and spread, not one number.
  python evals/retrieval_check.py [vector|hybrid|rerank] [--runs N]"""
import argparse
import statistics as st

from _common import GOLD

from brain import opensearch, retrieval

ap = argparse.ArgumentParser()
ap.add_argument("mode", nargs="?", default="rerank", choices=["vector", "hybrid", "rerank"])
ap.add_argument("--runs", type=int, default=1)
ap.add_argument("--extra", action="store_true", help="also score evals/extra_cases.json (25 more labelled questions)")
a = ap.parse_args()
K = 5
NEEDS_AGENT = {"SIMPLE_015", "SIMPLE_016", "SIMPLE_019"}  # meta / ambiguous / multi-hop
c = opensearch.client()
SETS = {
    "retrieval_expectations": [(x["case_id"], x["query"], x["expected_documents"]) for x in GOLD["retrieval_expectations"]],
    "single_turn_cases": [(x["case_id"], x["input"], [s for s in x["expected_sources"] if s.endswith(".md")])
                          for x in GOLD["single_turn_cases"]],
}
if a.extra:
    import json
    from pathlib import Path
    SETS["extra_cases"] = [(x["case_id"], x["query"], x["expected_documents"])
                           for x in json.loads((Path(__file__).parent / "extra_cases.json").read_text())]


def one_run(cases):
    per = {}
    for cid, q, exp in cases:
        if cid in NEEDS_AGENT or not exp:
            continue
        got = {h["source"].rsplit("/", 1)[-1] for h in retrieval.search(q, "E018", k=K, os_client=c, mode=a.mode)}
        per[cid] = 1 - len([e for e in exp if e.rsplit("/", 1)[-1] not in got]) / len(exp)
    return per


for label, cases in SETS.items():
    runs = [one_run(cases) for _ in range(a.runs)]
    means = [sum(r.values()) / len(r) for r in runs]
    line = f"{label} [{a.mode}] recall@{K} mean = {st.mean(means):.2f}"
    if a.runs > 1:
        line += f"  min {min(means):.2f}  max {max(means):.2f}  sd {st.pstdev(means):.2f}  ({a.runs} runs)"
    print(line)
    for cid in runs[0]:  # cases that are not perfect in every run are the unstable or failing ones
        vals = [r[cid] for r in runs]
        if min(vals) < 1:
            print(f"  {cid:14} recall per run: {', '.join(f'{v:.2f}' for v in vals)}")
print("needs agent follow-up (not scored):", sorted(NEEDS_AGENT))
