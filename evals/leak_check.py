"""Audience-leak check (live). Non-managers must never get a manager_playbook chunk.
  python evals/leak_check.py   (exit 1 on any leak)"""
import sys

from _common import GOLD  # noqa: F401

from server import opensearch, people, retrieval

c = opensearch.client()
QUERIES = ["how do I get promoted", "promotion cycle dates", "how do I run a performance review",
           "how to give feedback to my team", "salary bands and compensation conversations",
           "how do I handle an underperforming employee", "calibration process", "career ladder",
           "manager guide to terminations", "one on one meeting template", "how do I grow my career",
           "what is the promotion process timeline"]
assert not people.is_manager("E010") and not people.is_manager("E001") and people.is_manager("E018")
leaks = 0
for caller in ("E010", "E001"):
    for q in QUERIES:
        for h in retrieval.search(q, caller, k=10, os_client=c):
            if h["audience"] != "all" or h["corpus"] == "manager_playbook":
                leaks += 1
                print("LEAK", caller, q, h["source"])
print(f"non-manager leaks: {leaks}")
h = retrieval.search("how do I get promoted", "E010", k=5, os_client=c)
print("E010 sees career handbook:", any(x["source"].endswith("making-a-career.md") for x in h))
m = retrieval.search("how do I get promoted", "E018", k=10, os_client=c)
print("E018 can reach playbook:", any(x["corpus"] == "manager_playbook" for x in m))
sys.exit(1 if leaks or not any(x["corpus"] == "manager_playbook" for x in m) else 0)
