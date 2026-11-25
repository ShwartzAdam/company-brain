"""The only module that talks to Bedrock. A provider swap touches this file."""
import json

from brain import config

_backend = None  # tests install a fake via set_backend()


class BedrockBackend:
    def __init__(self):
        import boto3
        self._c = boto3.client("bedrock-runtime", region_name=config.REGION)

    def complete(self, messages, system, max_tokens, temperature):
        r = self._c.converse(
            modelId=config.MODEL_ID,
            system=[{"text": system}] if system else [],
            messages=[{"role": m["role"], "content": [{"text": m["content"]}]} for m in messages],
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
        )
        return r["output"]["message"]["content"][0]["text"]

    def embed(self, text):
        r = self._c.invoke_model(
            modelId=config.EMBED_MODEL_ID,
            body=json.dumps({"inputText": text, "dimensions": config.EMBED_DIM, "normalize": True}),
        )
        return json.loads(r["body"].read())["embedding"]


def set_backend(b):
    global _backend
    _backend = b


def _get():
    global _backend
    if _backend is None:
        _backend = BedrockBackend()
    return _backend


def complete(messages: list[dict], system: str = "", max_tokens: int = 1000, temperature: float = 0.2) -> str:
    """messages: [{"role": "user"|"assistant", "content": str}]"""
    return _get().complete(messages, system, max_tokens, temperature)


def embed(text: str) -> list[float]:
    return _get().embed(text)
