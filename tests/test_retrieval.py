from brain import retrieval


def _hit(i, corpus="employment", source=None):
    return {"_id": str(i), "_source": {"source": source or f"documents/{corpus}/f{i}.md", "text": "t",
                                       "corpus": corpus, "audience": "all", "section": ""}}


def test_reserve_policy_replaces_last_slots_with_policy_picks(fake_llm):
    fake_llm.reply = "[1, 0]"
    bodies = []

    def q(body):
        bodies.append(body)
        return [_hit(10 + len(bodies), "policies"), _hit(20 + len(bodies), "policies")]

    hits = [_hit(i) for i in range(5)]
    out = retrieval._reserve_policy("can a sales employee get access?", "E010", hits, q)
    assert len(out) == 5
    assert [h["_id"] for h in out[:3]] == ["0", "1", "2"]          # specific files keep the top slots
    assert all(h["_source"]["corpus"] == "policies" for h in out[3:])  # the rule gets the reserved ones
    # the policy search carries the caller's audience filter and the corpus filter
    for b in bodies:
        s = str(b)
        assert "'audience': 'all'" in s and "'corpus': 'policies'" in s


def test_reserve_policy_no_policy_candidates_leaves_hits(fake_llm):
    hits = [_hit(i) for i in range(5)]
    assert retrieval._reserve_policy("q", "E010", hits, lambda b: []) == hits
