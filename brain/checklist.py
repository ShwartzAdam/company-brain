"""OnboardingChecklist (spec 6.5): items with status and at least one citation, a reason for anything blocked,
and an overall status. Built by code from tool rows, never by the model, so an item is never 'completed'
unless the onboarding table says so and a blocker always names its evidence."""
from typing import Literal

from pydantic import BaseModel, Field


class ChecklistItem(BaseModel):
    task_id: str
    label: str
    status: str
    citations: list[str] = Field(min_length=1)
    blocked_reason: str = ""


class OnboardingChecklist(BaseModel):
    employee_id: str
    items: list[ChecklistItem]
    status: Literal["blocked", "pending", "completed"]


def _rows(evidence: list, tool: str):
    for e in evidence:
        if e["tool"] == tool and isinstance(e["result"], list):
            yield e, [r for r in e["result"] if isinstance(r, dict)]


def build(evidence: list) -> OnboardingChecklist | None:
    tasks = [(e["args"].get("employee_id", ""), r) for e, rows in _rows(evidence, "list_onboarding_tasks") for r in rows]
    if not tasks:
        return None
    subs = [r for _, rows in _rows(evidence, "check_software_subscription") for r in rows]
    contracts = [r for _, rows in _rows(evidence, "get_contract") for r in rows]
    items = []
    for _, t in tasks:
        cites, reason = [t["source"]], ""
        if t["status"] == "blocked":
            reason = "Blocked in the onboarding table."
            for s in subs:
                if s["system_name"].lower() in t["description"].lower():
                    cites.append(s["source"])
                    if s.get("over_limit"):
                        reason = (f"{s['system_name']} has {s['active_seats']} active seats against a contract limit of "
                                  f"{s['seat_limit']}, so no seat is available.")
                    for c in contracts:
                        if c["system_name"].lower() == s["system_name"].lower():
                            cites.append(c["source"])
        items.append(ChecklistItem(task_id=t["task_id"], label=t["description"], status=t["status"],
                                   citations=list(dict.fromkeys(cites)), blocked_reason=reason))
    status = ("blocked" if any(i.status == "blocked" for i in items)
              else "pending" if any(i.status != "completed" for i in items) else "completed")
    return OnboardingChecklist(employee_id=tasks[0][0], items=items, status=status)
