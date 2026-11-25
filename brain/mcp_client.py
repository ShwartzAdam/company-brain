"""The graph's only way to reach tools: an MCP session. Never imports brain.db/tools/retrieval.
caller_id is injected here from the application's argument, whatever the model wrote."""
import contextlib
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from brain import config


class Toolbox:
    def __init__(self, session: ClientSession, caller_id: str):
        self.s, self.caller_id, self.specs = session, caller_id, {}

    async def load(self) -> "Toolbox":
        self.specs = {t.name: t for t in (await self.s.list_tools()).tools}
        return self

    def describe(self, names: list[str]) -> str:
        """Prompt text for the visible tools only; caller_id is hidden from the model."""
        out = []
        for n in names:
            t = self.specs.get(n)
            if not t:
                continue
            props = {k: v.get("type", "any") for k, v in (t.inputSchema.get("properties") or {}).items()
                     if k not in ("caller_id", "as_of", "conversation_id")}
            out.append(f"- {n}({', '.join(f'{k}: {v}' for k, v in props.items())}): "
                       f"{(t.description or '').strip().splitlines()[0] if t.description else ''}")
        return "\n".join(out)

    async def call(self, name: str, args: dict):
        args = dict(args or {})
        if "caller_id" in (self.specs[name].inputSchema.get("properties") or {}):
            args["caller_id"] = self.caller_id
        res = await self.s.call_tool(name, args)
        if res.isError:
            raise RuntimeError(" ".join(getattr(c, "text", "") for c in res.content)[:300])
        parts = [json.loads(c.text) for c in res.content if getattr(c, "text", None)]
        return parts


@contextlib.asynccontextmanager
async def open_toolbox(caller_id: str, url: str | None = None):
    async with streamablehttp_client(url or config.MCP_URL) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            yield await Toolbox(s, caller_id).load()
