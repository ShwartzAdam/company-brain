"""Labeled retrieval check (live). recall@5 of expected DOCUMENTS (database tables are not
retrievable here, so they are not scored). Cases that need the agent's second search are
listed, not scored. Retrieval is not deterministic (approximate kNN + model rerank), so
use --runs N and read the mean and spread, not one number.
  python evals/retrieval_check.py [vector|hybrid|rerank] [--runs N] [--k N] [--extra] [--heldout]
Also prints the score with our corrected labels (evals/label_overrides.json); the golden file is never edited.
--heldout scores the DRAFT fresh questions (evals/heldout_cases.json): run once, review labels first, never tune on it."""
import argparse
import json
import statistics as st
from pathlib import Path

from _common import GOLD

from brain import opensearch, retrieval

ap = argparse.ArgumentParser()
ap.add_argument("mode", nargs="?", default="rerank", choices=["vector", "hybrid", "rerank"])
ap.add_argument("--runs", type=int, default=1)
ap.add_argument("--heldout", action="store_true", help="also score evals/heldout_cases.json (draft, run once, never tune on it)")
ap.add_argument("--k", type=int, default=5, help="documents returned per search (the agent tool defaults to 8)")
ap.add_argument("--extra", action="store_true", help="also score evals/extra_cases.json (25 more labelled questions)")
a = ap.parse_args()
K = a.k
NEEDS_AGENT = {"SIMPLE_015", "SIMPLE_016", "SIMPLE_019"}  # meta / ambiguous / multi-hop
c = opensearch.client()
SETS = {
    "retrieval_expectations": [(x["case_id"], x["query"], x["expected_documents"]) for x in GOLD["retrieval_expectations"]],
    "single_turn_cases": [(x["case_id"], x["input"], [s for s in x["expected_sources"] if s.endswith(".md")])
                          for x in GOLD["single_turn_cases"]],
}
if a.heldout:
    SETS["heldout_cases"] = [(x["case_id"], x["query"], x["expected_documents"])
                             for x in json.loads((Path(__file__).parent / "heldout_cases.json").read_text())["cases"]]
if a.extra:
    SETS["extra_cases"] = [(x["case_id"], x["query"], x["expected_documents"])
                           for x in json.loads((Path(__file__).parent / "extra_cases.json").read_text())]


OVERRIDES = {k: v for k, v in json.loads((Path(__file__).parent / "label_overrides.json").read_text()).items()
             if not k.startswith("_")}  # our corrected copy of a few labels; the golden file is untouched


def one_run(cases):
    """(per-case recall on the original labels, per-case recall with our corrected labels)."""
    per, adj = {}, {}
    for cid, q, exp in cases:
        if cid in NEEDS_AGENT or not exp:
            continue
        got = {h["source"].rsplit("/", 1)[-1] for h in retrieval.search(q, "E018", k=K, os_client=c, mode=a.mode)}
        per[cid] = adj[cid] = 1 - len([e for e in exp if e.rsplit("/", 1)[-1] not in got]) / len(exp)
        if cid in OVERRIDES:
            o = OVERRIDES[cid]
            got = {h["source"].rsplit("/", 1)[-1] for h in retrieval.search(q, "E018", k=o["k"], os_client=c, mode=a.mode)}
            adj[cid] = 1 - len([e for e in o["expected_documents"] if e not in got]) / len(o["expected_documents"])
    return per, adj


for label, cases in SETS.items():
    both = [one_run(cases) for _ in range(a.runs)]
    runs = [b[0] for b in both]
    means = [sum(r.values()) / len(r) for r in runs]
    line = f"{label} [{a.mode}] recall@{K} mean = {st.mean(means):.2f}"
    if a.runs > 1:
        line += f"  min {min(means):.2f}  max {max(means):.2f}  sd {st.pstdev(means):.2f}  ({a.runs} runs)"
    adj_means = [sum(b[1].values()) / len(b[1]) for b in both]
    if any(cid in OVERRIDES for cid in runs[0]):
        line += f"   | corrected labels (evals/label_overrides.json): {st.mean(adj_means):.2f}"
    print(line)
    for cid in runs[0]:  # cases that are not perfect in every run are the unstable or failing ones
        vals = [r[cid] for r in runs]
        if min(vals) < 1:
            print(f"  {cid:14} recall per run: {', '.join(f'{v:.2f}' for v in vals)}")
print("needs agent follow-up (not scored):", sorted(NEEDS_AGENT))
