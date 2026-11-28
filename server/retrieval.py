"""Retrieval: vector + BM25 over OpenSearch, fused by rank (RRF), then reranked by the model.
The audience filter lives inside EVERY query, so a restricted chunk never leaves the store
for a caller who may not see it (reranking only ever sees already-filtered chunks)."""
import json
import os
import re

from langfuse import get_client, observe

from brain import config, llm, people

SRC = ["text", "source", "corpus", "audience", "title", "section", "doc_type", "system"]
POOL = 30      # candidates fetched per query before fusion/rerank
RRF_K = 60
POLICY_POOL = 10
RESERVE_POLICY = True
RESERVED = 2# result slots reserved for the best policy chunks


def audience_filter(caller_id: str) -> list:
    return [] if people.is_manager(caller_id) else [{"term": {"audience": "all"}}]


def _filters(caller_id: str, corpus: str | None, extra: list | None = None) -> list:
    must = audience_filter(caller_id)
    if corpus:
        must.append({"term": {"corpus": corpus}})
    return must + (extra or [])


def build_body(query: str, caller_id: str, k: int, corpus: str | None, extra: list | None = None) -> dict:
    """Vector query body."""
    must = _filters(caller_id, corpus, extra)
    knn = {"vector": llm.embed(query), "k": k}
    if must:
        knn["filter"] = {"bool": {"must": must}}
    return {"size": k, "query": {"knn": {"vector": knn}}, "_source": SRC}


def build_keyword_body(query: str, caller_id: str, k: int, corpus: str | None, extra: list | None = None) -> dict:
    """BM25 query body with the same filters."""
    return {"size": k, "_source": SRC,
            "query": {"bool": {"must": [{"match": {"text": query}}], "filter": _filters(caller_id, corpus, extra)}}}


def rrf(rankings: list[list[dict]], k: int = RRF_K) -> list[dict]:
    score, by_id = {}, {}
    for ranking in rankings:
        for rank, h in enumerate(ranking, 1):
            score[h["_id"]] = score.get(h["_id"], 0) + 1 / (k + rank)
            by_id[h["_id"]] = h
    return [by_id[i] for i in sorted(score, key=score.get, reverse=True)]


RERANK_SYSTEM = ("You rank search results. Given a question and numbered passages, return ONLY a JSON "
                 "list of the passage numbers that help answer it, best first, at most {n} numbers. "
                 "Prefer the governing policy or rule over passages that merely mention the same names.")


def rerank(query: str, hits: list[dict], n: int, chars: int = 350) -> list[dict]:
    lines = [f"[{i}] {h['_source']['source']} | {h['_source']['text'][:chars]}" for i, h in enumerate(hits)]
    out = llm.complete([{"role": "user", "content": f"Question: {query}\n\n" + "\n".join(lines)}],
                       RERANK_SYSTEM.format(n=n), max_tokens=200, temperature=0)
    m = re.search(r"\[[\d,\s]*\]", out)
    try:
        ids = [i for i in json.loads(m.group(0)) if isinstance(i, int) and 0 <= i < len(hits)] if m else []
    except ValueError:
        ids = []
    picked = list(dict.fromkeys(ids))[:n]
    picked += [i for i in range(len(hits)) if i not in picked]  # fall back to fused order to fill k
    return [hits[i] for i in picked[:n]]


def _reserve_policy(query: str, caller_id: str, hits: list[dict], q) -> list[dict]:
    """Questions that name a person or product match files about them, not the rule that governs
    the answer. If no policy chunk made the cut, swap the last slot for the best policy chunk,
    found by a search restricted to corpus=policies (same audience filter)."""
    pol = rrf([q(build_body(query, caller_id, POLICY_POOL, "policies")),
               q(build_keyword_body(query, caller_id, POLICY_POOL, "policies"))])
    if not pol:
        return hits
    picks = rerank(query, pol[:POLICY_POOL], RESERVED)  # the model picks the governing rule among policies only
    have = {h["_id"] for h in picks}
    keep = [h for h in hits if h["_id"] not in have][:len(hits) - len(picks)]
    return keep + picks


MUST_CONTRACTS = 2
MUST_POLICIES = 2      # the model picks the governing policies among policies only
MUST_MEMOS = 1         # a memo supersedes policy text; kept only when it also scored in the general pool
MEMO_POOL_RANK = 15
TYPED = ("contract", "kb_article", "memo")  # variant c: the document types that answer a question about a system
_SYSTEMS: dict = {}


def _known_systems(q, caller_id: str) -> list[str]:
    """System names stored on the index (variants a+); empty on the baseline index. The audience filter
    applies here too, so a restricted document never contributes even a name to a caller who may not see it."""
    f = audience_filter(caller_id)
    key = bool(f)
    if key not in _SYSTEMS:
        try:
            body = {"size": 0, "aggs": {"s": {"terms": {"field": "system", "size": 200}}}}
            if f:
                body["query"] = {"bool": {"filter": f}}
            _SYSTEMS[key] = [b["key"] for b in q(body, raw=True)["aggregations"]["s"]["buckets"]]
        except Exception:
            return []
    return _SYSTEMS[key]


def _doc_candidates(query: str, caller_id: str, corpus: str | None, q) -> tuple[list[dict], list[dict]]:
    """Variant c candidate documents (one hit per document, best chunk): the usual fused pool, then the best
    contract / KB article / memo for each system the query names, then the best policies. Every query carries
    the same audience filter. A system name only picks which documents to search; it never decides access.
    Also returns `must`, which the model rerank may not drop: the best contract of each named system (at most 2),
    the 2 governing policies (the model picks them among policies only) and one memo that also scored in the pool."""
    pool = rrf([q(build_body(query, caller_id, POOL, corpus)), q(build_keyword_body(query, caller_id, POOL, corpus))])
    typed, must = [], []
    for sysname in [x for x in _known_systems(q, caller_id) if re.search(rf"\b{re.escape(x)}\b", query, re.I)]:
        ex = [{"term": {"system": sysname}}]
        found = rrf([q(build_body(query, caller_id, 10, corpus, ex)), q(build_keyword_body(query, caller_id, 10, corpus, ex))])
        for t in TYPED:
            typed += [h for h in found if h["_source"].get("doc_type") == t][:1]
        must += [h for h in found if h["_source"].get("doc_type") == "contract"][:1]
    pol = []
    if corpus is None:
        pol = rrf([q(build_body(query, caller_id, POLICY_POOL, "policies")),
                   q(build_keyword_body(query, caller_id, POLICY_POOL, "policies"))])[:POLICY_POOL]
    must = must[:MUST_CONTRACTS]
    if pol:
        must += rerank(query, pol[:POLICY_POOL], MUST_POLICIES, chars=1200)
    if corpus is None:
        memos = rrf([q(build_body(query, caller_id, POLICY_POOL, "internal_memos")),
                     q(build_keyword_body(query, caller_id, POLICY_POOL, "internal_memos"))])[:MUST_MEMOS]
        near = {h["_source"]["source"] for h in pool[:MEMO_POOL_RANK]}
        must += [m for m in memos if m["_source"]["source"] in near]
    docs, seen = [], set()
    for h in typed + pol[:3] + pool:  # first hit per document wins, coverage first
        if h["_source"]["source"] not in seen:
            seen.add(h["_source"]["source"])
            docs.append(h)
    return docs[:POOL], must


@observe(as_type="retriever", name="retrieval.search", capture_input=False, capture_output=False)
def search(query: str, caller_id: str, k: int = 5, corpus: str | None = None, os_client=None,
           mode: str = "rerank") -> list[dict]:
    """mode: vector | hybrid | rerank."""
    if os_client is None:
        from brain import opensearch
        os_client = opensearch.client()
    def q(body, raw=False):
        r = os_client.search(index=config.INDEX_NAME, body=body)
        return r if raw else r["hits"]["hits"]
    if mode == "rerank" and os.getenv("RETRIEVAL_VARIANT", "base") == "c":
        # `corpus` is a preference here, not a wall: a model that guesses the wrong corpus must still reach the
        # contract or policy that holds the answer. The audience filter stays in every query.
        docs, must = _doc_candidates(query, caller_id, None, q)
        if corpus:
            pref = rrf([q(build_body(query, caller_id, POOL, corpus)), q(build_keyword_body(query, caller_id, POOL, corpus))])
            seen = {d["_source"]["source"] for d in docs}
            docs = ([p for p in pref[:10] if p["_source"]["source"] not in seen] + docs)[:POOL]
        hits = rerank(query, docs, k, chars=1200)
        have = {h["_source"]["source"] for h in hits}
        extra = [h for h in dict((m["_source"]["source"], m) for m in must).values()
                 if h["_source"]["source"] not in have][:k - 1]
        if extra:  # fixed rule: the contract / policy slots are not left to the model; drop the lowest reranked
            fixed = {m["_source"]["source"] for m in must}
            for h in reversed(list(hits)):  # drop the lowest-ranked slots that are not themselves fixed
                if len(hits) + len(extra) <= k:
                    break
                if h["_source"]["source"] not in fixed:
                    hits.remove(h)
            hits = hits + extra
        if corpus:  # the requested corpus first, the rest after
            hits = sorted(hits, key=lambda h: h["_source"].get("corpus") != corpus)
        return _result(hits, query, caller_id, mode, k, corpus)
    if mode == "rerank" and corpus:
        # `corpus` is a preference, not a wall (the model guesses it, and a wrong guess such as 'contracts' for an offer letter
        # hid the letter completely): the preferred corpus feeds the pool first, the whole index feeds it too. The audience
        # filter is in every query, so widening never reaches a restricted document.
        pref = rrf([q(build_body(query, caller_id, POOL, corpus)), q(build_keyword_body(query, caller_id, POOL, corpus))])
        wide = rrf([q(build_body(query, caller_id, POOL, None)), q(build_keyword_body(query, caller_id, POOL, None))])
        pool = list({h["_id"]: h for h in pref[:POOL // 2] + wide}.values())[:POOL]
        hits = sorted(rerank(query, pool, k), key=lambda h: h["_source"].get("corpus") != corpus)  # preferred corpus first
        return _result(hits, query, caller_id, mode, k, corpus)
    vec = q(build_body(query, caller_id, POOL if mode != "vector" else k, corpus))
    if mode == "vector":
        hits = vec[:k]
    else:
        fused = rrf([vec, q(build_keyword_body(query, caller_id, POOL, corpus))])
        hits = fused[:k] if mode == "hybrid" else rerank(query, fused[:POOL], k)
        if mode == "rerank" and corpus is None and RESERVE_POLICY:
            hits = _reserve_policy(query, caller_id, hits, q)
    return _result(hits, query, caller_id, mode, k, corpus)


def _result(hits, query, caller_id, mode, k, corpus) -> list[dict]:
    res = [{**h["_source"], "score": round(h.get("_score") or 0, 4)} for h in hits]
    # Metadata shows what was filtered and which documents reached the model: paths, never text.
    get_client().update_current_span(
        input={"query": query, "caller_id": caller_id, "mode": mode, "k": k, "corpus": corpus},
        output=[{"source": r["source"], "section": r["section"], "audience": r["audience"]} for r in res],
        metadata={"audience_filter": _filters(caller_id, corpus), "caller_is_manager": not audience_filter(caller_id),
                  "pool": POOL, "sources_returned": sorted({r["source"] for r in res})})
    return res
