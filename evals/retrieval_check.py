"""Labeled retrieval check (live). recall@5 of expected DOCUMENTS (database tables are not
retrievable here, so they are not scored). Cases that need the agent's second search are
listed, not scored.   python evals/retrieval_check.py [vector|hybrid|rerank]"""
import sys

from _common import GOLD

from brain import opensearch, retrieval

MODE = sys.argv[1] if len(sys.argv) > 1 else "rerank"
K = 5
NEEDS_AGENT = {"SIMPLE_015", "SIMPLE_016", "SIMPLE_019"}  # meta / ambiguous / multi-hop
c = opensearch.client()


def run(label, cases):
    rs = []
    for cid, q, exp in cases:
        if cid in NEEDS_AGENT or not exp:
            continue
        got = {h["source"].rsplit("/", 1)[-1] for h in retrieval.search(q, "E018", k=K, os_client=c, mode=MODE)}
        miss = [e for e in exp if e.rsplit("/", 1)[-1] not in got]
        rs.append(1 - len(miss) / len(exp))
        if miss:
            print(f"  {cid:14} missing={[m.rsplit('/', 1)[-1] for m in miss]}")
    print(f"{label} [{MODE}] mean recall@{K} = {sum(rs) / len(rs):.2f} over {len(rs)} cases")
    return sum(rs) / len(rs)


run("retrieval_expectations", [(x["case_id"], x["query"], x["expected_documents"]) for x in GOLD["retrieval_expectations"]])
run("single_turn_cases", [(x["case_id"], x["input"], [s for s in x["expected_sources"] if s.endswith(".md")])
                          for x in GOLD["single_turn_cases"]])
print("needs agent follow-up (not scored):", sorted(NEEDS_AGENT))
