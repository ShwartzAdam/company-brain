"""NovaOps MCP server (localhost only). Every tool is a thin wrapper over brain.tools.
caller_id is an argument supplied by the application, never parsed from message text."""
import functools
import inspect

from langfuse import get_client
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.server import Settings as _S

from brain import config, tools

_S.model_rebuild()
mcp = FastMCP("novaops", host=config.MCP_HOST, port=config.MCP_PORT)

READ = [tools.search_documents, tools.get_employee, tools.list_direct_reports,
        tools.check_software_subscription, tools.get_contract, tools.list_onboarding_tasks,
        tools.check_asset_inventory, tools.list_employee_tickets, tools.read_approval,
        tools.list_access_requests, tools.seat_assignment_status]
WRITE = [tools.create_access_request, tools.create_ticket, tools.request_approval, tools.release_access,
         tools.record_consent]  # record_consent: called by application code only, in no loadout


def traced(fn):
    """Open `server.<tool>` under the client's `tool.<tool>` span (same trace) and tell release_write which request
    and trace caused any write. The client sends trace_id, parent_span_id and request_id as call metadata."""
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def run(*a, ctx: Context, **kw):
        m = getattr(ctx.request_context, "meta", None)
        meta = (m.model_extra if m else None) or {}
        link = {"trace_id": meta["trace_id"], "parent_span_id": meta["parent_span_id"]} if meta.get("trace_id") and meta.get("parent_span_id") else None
        lf, tok = get_client(), tools.REQUEST.set((meta.get("request_id", ""), meta.get("trace_id", "")))
        try:
            with lf.start_as_current_observation(name=f"server.{fn.__name__}", as_type="tool", trace_context=link,
                                                 metadata={"request_id": meta.get("request_id", ""), "process": "mcp-server"}) as sp:
                out = fn(*a, **kw)
                sp.update(output={"rows": len(out) if isinstance(out, list) else 1,
                                  **({"replayed": out["replayed"]} if isinstance(out, dict) and "replayed" in out else {})})
            if fn in WRITE:  # writes are rare and the evidence that matters: do not wait for the batch timer
                lf.flush()
            return out
        finally:
            tools.REQUEST.reset(tok)

    run.__signature__ = sig.replace(parameters=[*sig.parameters.values(),
                                                inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context)])
    run.__annotations__ = {**fn.__annotations__, "ctx": Context}
    return run


for fn in READ + WRITE:
    mcp.tool()(traced(fn))

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
