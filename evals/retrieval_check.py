"""Labeled retrieval check (live: OpenSearch + Titan). recall@5 of expected documents.
  python evals/retrieval_check.py"""
from _common import GOLD

from brain import opensearch, retrieval

K = 5
c = opensearch.client()
CALLER = "E018"  # sees everything; leak check covers restrictions


def recall(query, expected):
    got = {h["source"] for h in retrieval.search(query, CALLER, k=K, os_client=c)}
    got |= {s.rsplit("/", 1)[-1] for s in got}
    hit = [e for e in expected if e in got or e.rsplit("/", 1)[-1] in got]
    return len(hit) / len(expected), [e for e in expected if e not in hit]


tot = []
for case in GOLD["retrieval_expectations"]:
    r, miss = recall(case["query"], case["expected_documents"])
    tot.append(r)
    print(f"{case['case_id']:16} recall@{K}={r:.2f} missing={miss}")
print(f"MEAN retrieval_expectations recall@{K} = {sum(tot) / len(tot):.2f}")

tot = []
for case in GOLD["single_turn_cases"]:
    exp = case["expected_sources"]
    if not exp:
        continue
    r, miss = recall(case["input"], exp)
    tot.append(r)
    if miss:
        print(f"{case['case_id']:12} recall@{K}={r:.2f} missing={miss}")
print(f"MEAN single_turn_cases recall@{K} = {sum(tot) / len(tot):.2f} over {len(tot)} cases")
