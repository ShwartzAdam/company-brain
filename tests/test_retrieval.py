from server import retrieval


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
    from server import tools
    for who, is_mgr in (("E001", False), ("E010", False), ("E018", True)):
        cap = _Capture()
        retrieval.search("what does the manager's guide say about feedback", who, k=5, corpus="manager_playbook", os_client=cap)
        assert cap.bodies
        for b in cap.bodies:
            s = str(b)
            assert ("'audience': 'all'" in s) != is_mgr, (who, s)
    # the tool path: an unknown or hallucinated corpus is dropped, the audience filter is not
    cap = _Capture()
    import server.opensearch as os_mod
    orig = os_mod.client
    os_mod.client = lambda: cap
    try:
        tools.search_documents("promotion", "E001", corpus="manager_playbook")
        tools.search_documents("promotion", "E001", corpus="made_up")
    finally:
        os_mod.client = orig
    assert cap.bodies and all("'audience': 'all'" in str(b) for b in cap.bodies)


def test_variant_c_keeps_best_contract_and_policy(monkeypatch):
    """RETRIEVAL_VARIANT=c: the rerank may not drop the fixed contract / policy slots."""
    docs = [_hit(i) for i in range(6)]
    contract, policy = _hit(7, "contracts"), _hit(8, "policies")
    monkeypatch.setenv("RETRIEVAL_VARIANT", "c")
    monkeypatch.setattr(retrieval, "_doc_candidates", lambda *a: (docs + [contract, policy], [contract, policy]))
    monkeypatch.setattr(retrieval, "rerank", lambda q, hits, n, chars=350: docs[:n])
    monkeypatch.setattr(retrieval, "_result", lambda hits, *a: [h["_id"] for h in hits])

    class C:
        def search(self, **kw):
            return {"hits": {"hits": []}}

    out = retrieval.search("q", "E010", k=5, os_client=C())
    assert len(out) == 5 and {"7", "8"} <= set(out) and out[:3] == ["0", "1", "2"]


def test_variant_c_corpus_is_a_preference_not_a_wall(monkeypatch, fake_llm):
    """A model that asks for corpus=policies must still be able to receive the contract; audience stays hard."""
    seen = {}
    contract = _hit(7, "contracts")

    def cand(query, caller, corpus, q):
        seen["corpus"] = corpus
        return [contract] + [_hit(i, "policies") for i in range(6)], []

    monkeypatch.setenv("RETRIEVAL_VARIANT", "c")
    monkeypatch.setattr(retrieval, "_doc_candidates", cand)
    monkeypatch.setattr(retrieval, "rerank", lambda q, hits, n, chars=350: hits[:n])
    monkeypatch.setattr(retrieval, "_result", lambda hits, *a: [h["_source"]["corpus"] for h in hits])

    class C:
        def search(self, **kw):
            return {"hits": {"hits": [_hit(20, "policies")]}}

    out = retrieval.search("q", "E010", k=3, corpus="policies", os_client=C())
    assert seen["corpus"] is None and out[0] == "policies" and "contracts" in out


class _Corpora:
    """Fake index: a corpus-filtered query only sees that corpus; an unfiltered one sees everything (best match first)."""
    def __init__(self):
        self.bodies = []
        self.docs = [_hit(1, "contracts", "documents/contracts/bamboohr.md"), _hit(2, "contracts", "documents/contracts/deel.md"),
                     _hit(3, "employment", "documents/employment/maya_cohen_offer_letter.md")]

    def search(self, index, body):
        self.bodies.append(body)
        only = "contracts" if "{'term': {'corpus': 'contracts'}}" in str(body) else None
        return {"hits": {"hits": [d for d in self.docs if only in (None, d["_source"]["corpus"])]}}


def test_a_wrong_corpus_guess_does_not_hide_the_document(fake_llm):
    fake_llm.reply = "[2, 0, 1]"  # the model reranker puts the offer letter first
    cap = _Corpora()
    out = retrieval.search("Maya Cohen offer letter day one", "E004", k=5, corpus="contracts", os_client=cap)
    srcs = [h["source"] for h in out]
    assert "documents/employment/maya_cohen_offer_letter.md" in srcs  # the old hard filter returned only contracts
    assert srcs[0].startswith("documents/contracts/")                 # the preferred corpus still comes first
    assert any("{'term': {'corpus': 'contracts'}}" in str(b) for b in cap.bodies)  # the preferred corpus was searched ...
    assert any("{'term': {'corpus'" not in str(b) for b in cap.bodies)               # ... and so was the whole index


def test_widening_a_corpus_never_widens_the_audience(fake_llm):
    for who, is_mgr in (("E010", False), ("E018", True)):
        cap = _Corpora()
        retrieval.search("guide", who, k=5, corpus="manager_playbook", os_client=cap)
        assert cap.bodies and all(("'audience': 'all'" in str(b)) != is_mgr for b in cap.bodies), who
