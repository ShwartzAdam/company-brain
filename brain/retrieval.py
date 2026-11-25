"""Retrieval: vector + BM25 over OpenSearch, fused by rank (RRF), then reranked by the model.
The audience filter lives inside EVERY query, so a restricted chunk never leaves the store
for a caller who may not see it (reranking only ever sees already-filtered chunks)."""
import json
import re

from langfuse import get_client, observe

from brain import config, llm, people

SRC = ["text", "source", "corpus", "audience", "title", "section"]
POOL = 30      # candidates fetched per query before fusion/rerank
RRF_K = 60


def audience_filter(caller_id: str) -> list:
    return [] if people.is_manager(caller_id) else [{"term": {"audience": "all"}}]


def _filters(caller_id: str, corpus: str | None) -> list:
    must = audience_filter(caller_id)
    if corpus:
        must.append({"term": {"corpus": corpus}})
    return must


def build_body(query: str, caller_id: str, k: int, corpus: str | None) -> dict:
    """Vector query body."""
    must = _filters(caller_id, corpus)
    knn = {"vector": llm.embed(query), "k": k}
    if must:
        knn["filter"] = {"bool": {"must": must}}
    return {"size": k, "query": {"knn": {"vector": knn}}, "_source": SRC}


def build_keyword_body(query: str, caller_id: str, k: int, corpus: str | None) -> dict:
    """BM25 query body with the same filters."""
    return {"size": k, "_source": SRC,
            "query": {"bool": {"must": [{"match": {"text": query}}], "filter": _filters(caller_id, corpus)}}}


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


def rerank(query: str, hits: list[dict], n: int) -> list[dict]:
    lines = [f"[{i}] {h['_source']['source']} | {h['_source']['text'][:350]}" for i, h in enumerate(hits)]
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


@observe(as_type="retriever", name="retrieval.search", capture_input=False, capture_output=False)
def search(query: str, caller_id: str, k: int = 5, corpus: str | None = None, os_client=None,
           mode: str = "rerank") -> list[dict]:
    """mode: vector | hybrid | rerank."""
    if os_client is None:
        from brain import opensearch
        os_client = opensearch.client()
    q = lambda body: os_client.search(index=config.INDEX_NAME, body=body)["hits"]["hits"]  # noqa: E731
    vec = q(build_body(query, caller_id, POOL if mode != "vector" else k, corpus))
    if mode == "vector":
        hits = vec[:k]
    else:
        fused = rrf([vec, q(build_keyword_body(query, caller_id, POOL, corpus))])
        hits = fused[:k] if mode == "hybrid" else rerank(query, fused[:POOL], k)
    res = [{**h["_source"], "score": round(h.get("_score") or 0, 4)} for h in hits]
    # Metadata shows what was filtered and which documents reached the model: paths, never text.
    get_client().update_current_span(
        input={"query": query, "caller_id": caller_id, "mode": mode, "k": k, "corpus": corpus},
        output=[{"source": r["source"], "section": r["section"], "audience": r["audience"]} for r in res],
        metadata={"audience_filter": _filters(caller_id, corpus), "caller_is_manager": not audience_filter(caller_id),
                  "pool": POOL, "sources_returned": sorted({r["source"] for r in res})})
    return res
