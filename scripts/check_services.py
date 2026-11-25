"""Step 0 check: prove each external service works before building on it.

Run: python scripts/check_services.py   (reads .env; prints PASS/FAIL per service, never secrets)
"""
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()
REGION = os.getenv("AWS_REGION", "us-east-1")
results = {}


def check(name):
    def wrap(fn):
        try:
            results[name] = ("PASS", fn())
        except Exception as e:  # report the failure, keep going
            results[name] = ("FAIL", f"{type(e).__name__}: {str(e)[:160]}")
        return fn
    return wrap


@check("env vars set")
def _():
    need = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "BEDROCK_MODEL_ID", "BEDROCK_EMBEDDING_MODEL_ID",
            "OPENSEARCH_COLLECTION", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"]
    missing = [k for k in need if not os.getenv(k)]
    if missing:
        raise RuntimeError("missing: " + ", ".join(missing))
    return "all present"


@check("Bedrock Nova 2 Lite reply")
def _():
    import boto3
    r = boto3.client("bedrock-runtime", region_name=REGION).converse(
        modelId=os.environ["BEDROCK_MODEL_ID"],
        messages=[{"role": "user", "content": [{"text": "Reply with the single word: ok"}]}],
        inferenceConfig={"maxTokens": 20})
    return r["output"]["message"]["content"][0]["text"].strip()[:40]


@check("Titan embedding length")
def _():
    import boto3
    r = boto3.client("bedrock-runtime", region_name=REGION).invoke_model(
        modelId=os.environ["BEDROCK_EMBEDDING_MODEL_ID"], body=json.dumps({"inputText": "hello"}))
    return f"{len(json.loads(r['body'].read())['embedding'])} dims"


@check("OpenSearch Serverless collection reachable")
def _():
    import boto3
    from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection
    name = os.environ["OPENSEARCH_COLLECTION"]
    c = boto3.client("opensearchserverless", region_name=REGION).batch_get_collection(names=[name])
    if not c["collectionDetails"]:
        raise RuntimeError(f"collection {name!r} not found")
    d = c["collectionDetails"][0]
    host = d["collectionEndpoint"].replace("https://", "")
    auth = AWSV4SignerAuth(boto3.Session().get_credentials(), REGION, "aoss")
    OpenSearch(hosts=[{"host": host, "port": 443}], http_auth=auth, use_ssl=True,
               connection_class=RequestsHttpConnection).cat.indices()
    return f"status {d['status']}"


@check("Langfuse auth")
def _():
    from langfuse import Langfuse
    if not Langfuse().auth_check():
        raise RuntimeError("auth_check returned False")
    return "authenticated"


for k, (s, m) in results.items():
    print(f"{s:4}  {k}: {m}")
sys.exit(0 if all(s == "PASS" for s, _ in results.values()) else 1)
