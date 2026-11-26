"""NovaOps MCP server (localhost only). Every tool is a thin wrapper over brain.tools.
caller_id is an argument supplied by the application, never parsed from message text."""
from mcp.server.fastmcp import FastMCP
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
for fn in READ + WRITE:
    mcp.tool()(fn)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
