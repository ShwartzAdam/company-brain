"""Filtered kNN. The audience filter lives inside the query, so a restricted chunk
never leaves the store for a caller who may not see it."""
from brain import config, llm, people

SRC = ["text", "source", "corpus", "audience", "title", "section"]


def audience_filter(caller_id: str) -> list:
    return [] if people.is_manager(caller_id) else [{"term": {"audience": "all"}}]


def build_body(query: str, caller_id: str, k: int, corpus: str | None) -> dict:
    must = audience_filter(caller_id)
    if corpus:
        must.append({"term": {"corpus": corpus}})
    knn = {"vector": llm.embed(query), "k": k}
    if must:
        knn["filter"] = {"bool": {"must": must}}
    return {"size": k, "query": {"knn": {"vector": knn}}, "_source": SRC}


def search(query: str, caller_id: str, k: int = 5, corpus: str | None = None, os_client=None) -> list[dict]:
    if os_client is None:
        from brain import opensearch
        os_client = opensearch.client()
    hits = os_client.search(index=config.INDEX_NAME, body=build_body(query, caller_id, k, corpus))["hits"]["hits"]
    return [{**h["_source"], "score": round(h["_score"], 4)} for h in hits]
