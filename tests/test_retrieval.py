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


class _Capture:
    def __init__(self):
        self.bodies = []

    def search(self, index, body):
        self.bodies.append(body)
        return {"hits": {"hits": []}}


def test_playbook_unreachable_for_non_manager_even_when_named_and_open_for_a_manager(fake_llm):
    """Fail closed at retrieval: every query body a non-manager sends carries audience=all, whatever corpus is asked for."""
    from brain import tools
    for who, is_mgr in (("E001", False), ("E010", False), ("E018", True)):
        cap = _Capture()
        retrieval.search("what does the manager's guide say about feedback", who, k=5, corpus="manager_playbook", os_client=cap)
        assert cap.bodies
        for b in cap.bodies:
            s = str(b)
            assert ("'audience': 'all'" in s) != is_mgr, (who, s)
    # the tool path: an unknown or hallucinated corpus is dropped, the audience filter is not
    cap = _Capture()
    import brain.opensearch as os_mod
    orig = os_mod.client
    os_mod.client = lambda: cap
    try:
        tools.search_documents("promotion", "E001", corpus="manager_playbook")
        tools.search_documents("promotion", "E001", corpus="made_up")
    finally:
        os_mod.client = orig
    assert cap.bodies and all("'audience': 'all'" in str(b) for b in cap.bodies)
