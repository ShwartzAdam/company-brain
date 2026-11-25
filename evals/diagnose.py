"""Why is an expected document missed? Rank of each expected .md in the top-50 vector hits,
and what outranked it. python evals/diagnose.py"""
from _common import GOLD

from brain import opensearch, retrieval

c = opensearch.client()
cases = [(x["case_id"], x["query"], x["expected_documents"]) for x in GOLD["retrieval_expectations"]]
cases += [(x["case_id"], x["input"], [s for s in x["expected_sources"] if s.endswith(".md")])
          for x in GOLD["single_turn_cases"]]
for cid, q, exp in cases:
    hits = retrieval.search(q, "E018", k=50, os_client=c)
    order = []
    for h in hits:  # first rank per document
        if h["source"] not in order:
            order.append(h["source"])
    miss = []
    for e in exp:
        m = [i for i, s in enumerate(order, 1) if s == e or s.endswith("/" + e.rsplit("/", 1)[-1])]
        if not m or m[0] > 5:
            miss.append((e.rsplit("/", 1)[-1], m[0] if m else ">50 chunks"))
    if miss:
        print(f"\n{cid}: {q!r}\n  missed(rank by doc): {miss}\n  top5: {[s.rsplit('/', 1)[-1] for s in order[:5]]}")
