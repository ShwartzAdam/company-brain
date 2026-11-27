"""The graph's only way to reach tools: an MCP session. Never imports brain.db/tools/retrieval.
caller_id is injected here from the application's argument, whatever the model wrote."""
import asyncio
import contextlib
import json

from langfuse import get_client
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from brain import config


class Toolbox:
    def __init__(self, session: ClientSession, caller_id: str):
        self.s, self.caller_id, self.specs = session, caller_id, {}
        self.ctx: dict = {}   # as_of, conversation_id: set by the application per turn, never by the model

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

    def missing_args(self, name: str, args: dict) -> list[str]:
        """Required arguments the application does not inject itself and the model left out."""
        sch = self.specs[name].inputSchema
        auto = {"caller_id", "as_of", "conversation_id"}
        return [k for k in sch.get("required") or [] if k not in auto and (args or {}).get(k) in (None, "")]

    async def call(self, name: str, args: dict):
        args = {k: v for k, v in (args or {}).items() if v is not None}  # a model's explicit nulls mean 'not given'
        props = self.specs[name].inputSchema.get("properties") or {}
        if "caller_id" in props:
            args["caller_id"] = self.caller_id
        for k in ("as_of", "conversation_id"):  # overwrite whatever the model wrote
            if k in props and k in self.ctx:
                args[k] = self.ctx[k]
        lf = get_client()
        meta = {"request_id": self.ctx.get("request_id")}
        shown = {k: ("<redacted>" if "token" in k else v) for k, v in args.items()}  # a consent token never enters a trace
        with lf.start_as_current_observation(name=f"tool.{name}", as_type="tool", input={"args": shown},
                                             metadata={**meta, "caller_id": self.caller_id}) as sp:
            # the server reads these and opens server.<tool> under THIS span, in the same trace
            meta |= {"trace_id": lf.get_current_trace_id(), "parent_span_id": lf.get_current_observation_id()}
            try:
                res = await asyncio.wait_for(self.s.call_tool(name, args, meta={k: v for k, v in meta.items() if v}),
                                             config.TOOL_TIMEOUT_S)  # a hung tool ends this call, not the run
                if res.isError:
                    raise RuntimeError(" ".join(getattr(c, "text", "") for c in res.content)[:300])
            except asyncio.TimeoutError:
                sp.update(level="ERROR", status_message=f"tool {name} timed out after {config.TOOL_TIMEOUT_S}s")
                raise TimeoutError(f"tool {name} timed out after {config.TOOL_TIMEOUT_S}s") from None
            except Exception as e:
                sp.update(level="ERROR", status_message=str(e)[:300])
                raise
            parts = [json.loads(c.text) for c in res.content if getattr(c, "text", None)]
            sp.update(output={"sources": [p["source"] for p in parts if isinstance(p, dict) and p.get("source")], "n": len(parts)})
            return parts


@contextlib.asynccontextmanager
async def open_toolbox(caller_id: str, url: str | None = None):
    async with streamablehttp_client(url or config.MCP_URL) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            yield await Toolbox(s, caller_id).load()
