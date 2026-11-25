"""The only module that talks to Bedrock. A provider swap touches this file."""
import json
import re

import jsonschema

from langfuse import get_client, observe

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
        u = r.get("usage", {})
        self.last_usage = {"input": u.get("inputTokens", 0), "output": u.get("outputTokens", 0)}
        return r["output"]["message"]["content"][0]["text"]

    def structured(self, messages, system, name, schema, max_tokens, temperature):
        """Forced tool call (lesson 3/7): the model must answer with arguments for one tool whose schema we set."""
        r = self._c.converse(
            modelId=config.MODEL_ID,
            system=[{"text": system}] if system else [],
            messages=[{"role": m["role"], "content": [{"text": m["content"]}]} for m in messages],
            toolConfig={"tools": [{"toolSpec": {"name": name, "description": "Return the result.",
                                                "inputSchema": {"json": schema}}}],
                        "toolChoice": {"tool": {"name": name}}},
            inferenceConfig={"maxTokens": max_tokens, "temperature": temperature})
        u = r.get("usage", {})
        self.last_usage = {"input": u.get("inputTokens", 0), "output": u.get("outputTokens", 0)}
        for block in r["output"]["message"]["content"]:
            if "toolUse" in block:
                return block["toolUse"]["input"]
        raise ValueError("model returned no tool call")

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


@observe(as_type="generation", name="llm.complete", capture_input=False, capture_output=False)
def complete(messages: list[dict], system: str = "", max_tokens: int = 1000, temperature: float = 0.2) -> str:
    """messages: [{"role": "user"|"assistant", "content": str}]. Traced as a Langfuse generation."""
    b = _get()
    out = b.complete(messages, system, max_tokens, temperature)
    usage = getattr(b, "last_usage", None)
    get_client().update_current_generation(
        input=[{"role": "system", "content": system}] + messages, output=out, model=config.MODEL_ID,
        model_parameters={"max_tokens": max_tokens, "temperature": temperature},
        usage_details=usage)
    return out


def _json_from_text(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in reply")
    return json.loads(m.group(0))


@observe(as_type="generation", name="llm.structured", capture_input=False, capture_output=False)
def structured(messages: list[dict], system: str, schema: dict, name: str = "result", max_tokens: int = 500,
               temperature: float = 0.0, retries: int = 1) -> dict:
    """Schema-valid JSON or ValueError. Bedrock: forced tool call. A backend without `structured` (the test
    fakes) answers through complete() and the JSON is pulled from its text; the schema check is the same."""
    b, last = _get(), None
    for _ in range(retries + 1):
        try:
            if hasattr(b, "structured"):
                out = b.structured(messages, system, name, schema, max_tokens, temperature)
            else:
                out = _json_from_text(b.complete(messages, system, max_tokens, temperature))
            jsonschema.validate(out, schema)
            get_client().update_current_generation(
                input=[{"role": "system", "content": system}] + messages, output=out, model=config.MODEL_ID,
                model_parameters={"max_tokens": max_tokens, "temperature": temperature, "forced_tool": name},
                usage_details=getattr(b, "last_usage", None))
            return out
        except (ValueError, jsonschema.ValidationError) as e:  # retry only output problems, not API errors
            last = e
    raise ValueError(f"no valid {name}: {str(last)[:160]}")


def embed(text: str) -> list[float]:
    return _get().embed(text)
