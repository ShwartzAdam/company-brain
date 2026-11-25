"""Scope: pure code, no model. Maps the classified intent to a scope and the tools visible this turn.
Adapted from lesson-10 stage02_policy.py LOADOUTS/select_tools. Visibility is not authorization:
write tools are added only through the gate, and the gate releases nothing yet."""
from brain.plan import TurnPlan

WRITE_TOOLS = {"create_access_request", "create_ticket", "request_approval"}
READ_TOOLS = ["search_documents", "get_employee", "list_direct_reports", "check_software_subscription",
              "get_contract", "list_onboarding_tasks", "check_asset_inventory", "list_employee_tickets",
              "read_approval"]

LOADOUTS: dict[str, tuple[str, list[str]]] = {
    "policy_question": ("maya", ["search_documents"]),
    "employee_lookup": ("maya", ["get_employee"]),
    "onboarding_status": ("maya", ["get_employee", "list_onboarding_tasks", "search_documents",
                                   "check_asset_inventory", "check_software_subscription", "get_contract"]),
    "equipment_request": ("maya", ["get_employee", "check_asset_inventory", "search_documents"]),
    "other": ("maya", READ_TOOLS),  # relevance fails open (lesson 10): an unrecognised turn sees every read tool
    "subscription_review": ("webex", ["check_software_subscription", "get_contract", "search_documents"]),
    "access_request": ("webex", ["get_employee", "check_software_subscription", "list_employee_tickets",
                                 "read_approval", "search_documents"]),
    "ticket_status": ("webex", ["get_employee", "list_employee_tickets"]),
    "team_question": ("webex", ["get_employee", "list_direct_reports", "check_software_subscription"]),
}


def select_tools(plan: TurnPlan, gate_open: bool = False) -> tuple[str, list[str]]:
    """-> (scope, visible tool names). Maya's scope can never hold a write tool, even if gate_open."""
    if plan.intent == "ambiguous":
        return "none", []
    scope, tools = LOADOUTS[plan.intent]
    if not plan.requires_tools:
        return scope, []
    tools = list(tools)
    if gate_open and scope == "webex" and plan.intent == "access_request":
        tools += sorted(WRITE_TOOLS)
    return scope, tools


def widen_reads(visible: list[str]) -> list[str]:
    """Rearm: every read tool comes back, writes only if they were already visible."""
    return READ_TOOLS + [t for t in visible if t in WRITE_TOOLS]
