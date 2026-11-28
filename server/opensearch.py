import boto3
from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

from brain import config

INDEX_BODY = {
    "settings": {"index": {"knn": True}},
    "mappings": {"properties": {
        "vector": {"type": "knn_vector", "dimension": config.EMBED_DIM,
                   "method": {"name": "hnsw", "space_type": "innerproduct"}},
        "text": {"type": "text"},
        "source": {"type": "keyword"},     # documents/<corpus>/<file>.md
        "corpus": {"type": "keyword"},
        "audience": {"type": "keyword"},   # all | manager (missing front matter => all)
        "title": {"type": "keyword"},
        "section": {"type": "keyword"},
        "doc_type": {"type": "keyword"},   # variants a+: contract | kb_article | policy | memo | ...
        "system": {"type": "keyword"},     # variants a+: Webex, Salesforce, ... (may be several)
    }},
}


def client() -> OpenSearch:
    sess = boto3.Session(region_name=config.REGION)
    d = sess.client("opensearchserverless").batch_get_collection(
        names=[config.OPENSEARCH_COLLECTION])["collectionDetails"]
    if not d:
        raise SystemExit(f"collection {config.OPENSEARCH_COLLECTION!r} not found")
    host = d[0]["collectionEndpoint"].replace("https://", "")
    return OpenSearch(hosts=[{"host": host, "port": 443}],
                      http_auth=AWSV4SignerAuth(sess.get_credentials(), config.REGION, "aoss"),
                      use_ssl=True, verify_certs=True, connection_class=RequestsHttpConnection,
                      timeout=120, max_retries=3, retry_on_timeout=True)


def recreate_index(c: OpenSearch) -> None:
    if c.indices.exists(index=config.INDEX_NAME):
        c.indices.delete(index=config.INDEX_NAME)
    c.indices.create(index=config.INDEX_NAME, body=INDEX_BODY)
