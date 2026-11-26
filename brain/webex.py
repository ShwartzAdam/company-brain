"""The Webex workflow: diagnose, reuse, dispatch approvals, then release only against recorded approvals.
Reaches tools only through the Toolbox (MCP). The typed AccessDecision is built by code from tool results:
a model never decides what was done, and no field here can say access was granted."""
from datetime import date
from typing import Literal

from langfuse import get_client
from pydantic import BaseModel, Field, model_validator

from brain import gate
from brain.handoff import PendingAccessResult, WebexHandoff
from brain.status import _GRANT, STATUSES


class Fact(BaseModel):
    text: str
    source: str


class AccessDecision(BaseModel):
    """Observed facts, actions actually taken, recommended next steps, the blocker, the status (spec 6.5)."""
    status: Literal["completed", "blocked", "pending", "needs_human", "failed"]
    blocker: Literal["seat_limit", "needs_approval", "none"] = "none"
    blocking_reason: str = ""
    observed_facts: list[Fact] = Field(default_factory=list)
    actions_taken: list[str] = Field(default_factory=list)
    recommended_next_steps: list[str] = Field(default_factory=list)
    reused: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def never_granted(self):
        text = " ".join([self.blocking_reason, *self.actions_taken, *self.recommended_next_steps,
                         *(f.text for f in self.observed_facts)])
        assert self.status in STATUSES and not _GRANT.search(text), "an AccessDecision cannot claim access was granted"
        return self

    def render(self) -> str:
        cite = lambda f: f"{f.text} [{f.source}]"  # noqa: E731
        out = ["Observed: " + "; ".join(cite(f) for f in self.observed_facts) + "."] if self.observed_facts else []
        if self.blocking_reason:
            out.append(f"Blocked by: {self.blocking_reason}.")
        out.append("Actions taken: " + ("; ".join(self.actions_taken) or "none") + ".")
        out.append("Next: " + "; ".join(self.recommended_next_steps) + ". Nothing has been granted.")
        return " ".join(out)


def _match(name: str, other: str) -> bool:
    return name.lower() in other.lower() or other.lower() in name.lower()


class WebexWorkflow:
    """The real Webex port. A request is only acted on when a request record already exists to reuse
    (so a first-ever request, such as Maya's, stays a pending handoff and writes nothing)."""

    def __init__(self, box):
        self.box = box

    async def _read(self, tool, **args):
        return [r for r in await self.box.call(tool, args) if isinstance(r, dict)]

    async def request_access(self, h: WebexHandoff) -> PendingAccessResult:
        ctx = self.box.ctx
        sub = (await self._read("check_software_subscription", software=h.system) or [{}])[0]
        ars = [r for r in await self._read("list_access_requests", employee_id=h.subject_employee_id)
               if _match(h.system, r["system_name"]) and r["status"] not in ("rejected", "denied", "cancelled")]
        if not ars:
            d = AccessDecision(status="pending", blocker="needs_approval",
                               blocking_reason="no recorded access request exists yet to act on",
                               actions_taken=[], recommended_next_steps=["IT files the request through the approval workflow"])
            return PendingAccessResult(idempotency_key=h.idempotency_key, decision=d.model_dump())
        ar = ars[0]
        tickets = [t for t in await self._read("list_employee_tickets", employee_id=h.subject_employee_id)
                   if t.get("related_system_id") == ar["system_id"] and t["status"] not in ("closed", "resolved")]
        consent = (await self._read("record_consent", employee_id=h.subject_employee_id, system=h.system,
                                    message_id=ctx["request_id"], draft=h.justification,
                                    consent_token=gate.consent_token(self.box.caller_id, h.subject_employee_id, h.system,
                                                                     ctx["request_id"], h.justification)))[0]["consent_id"]
        filed = (await self._read("create_access_request", employee_id=h.subject_employee_id, system=h.system,
                                  justification=h.justification, consent_id=consent))[0]
        actions = ([f"replay: request {filed['entity_id']} already recorded for this conversation, nothing new written"]
                   if filed.get("replayed") else
                   [f"reused existing request {filed['entity_id']} (status {filed['status']}), no new request created"
                    if filed.get("reused") else f"request {filed['entity_id']} recorded"])
        if tickets:
            actions.append(f"reused existing ticket {tickets[0]['ticket_id']}, no new ticket created")
        aps = await self._read("read_approval", request_id=filed["entity_id"])
        moved = []
        for ap in aps:
            if ap["status"] == "needed":
                r = (await self._read("request_approval", approval_id=ap["approval_id"], consent_id=consent))[0]
                moved.append(ap["approval_id"]) if r.get("transitioned") else None
        aps = await self._read("read_approval", request_id=filed["entity_id"])
        if moved:
            actions.append(f"requested approvals {', '.join(moved)} (needed -> pending)")
        facts = [Fact(text=f"{ar['system_name']} has {sub['active_seats']} active seats against a seat limit of {sub['seat_limit']}",
                      source=sub["source"])] if sub else []
        if sub.get("renewal_date"):
            days = (date.fromisoformat(sub["renewal_date"][:10]) - date.fromisoformat(ctx["as_of"][:10])).days
            facts.append(Fact(text=f"the contract renews {sub['renewal_date']} ({days} days from {ctx['as_of'][:10]})", source=sub["source"]))
        facts += [Fact(text=f"{t['ticket_id']} is {t['status']}", source=t["source"]) for t in tickets[:1]]
        facts += [Fact(text=f"{ar['request_id']} is {ar['status']}", source=ar["source"])]
        facts += [Fact(text=f"{a['approval_id']} ({a['request_type']}, approver {a['approver_id']}) is {a['status']}", source=a["source"]) for a in aps]
        over = bool(sub.get("over_limit"))
        waiting = [a["approval_id"] for a in aps if a["status"] != "approved"]
        d = AccessDecision(
            status="pending" if waiting else "needs_human", blocker="seat_limit" if over else ("needs_approval" if waiting else "none"),
            blocking_reason=(f"seat limit exceeded ({sub['active_seats']} active of {sub['seat_limit']})" if over else
                             "approvals not yet recorded") if (over or waiting) else "",
            observed_facts=facts, actions_taken=actions, reused=[filed["entity_id"], *[t["ticket_id"] for t in tickets[:1]]],
            recommended_next_steps=[f"approvers decide {', '.join(waiting)}" if waiting else "IT assigns a seat once capacity exists",
                                    "expand the seat limit or reclaim inactive seats"])
        pending = ({"request_id": filed["entity_id"], "approval_ids": [a["approval_id"] for a in aps], "consent_id": consent,
                    "employee_id": h.subject_employee_id, "system": h.system, "released": False} if waiting else None)
        return PendingAccessResult(idempotency_key=h.idempotency_key, decision=d.model_dump(), pending=pending,
                                   note="Approvals requested; nothing is approved or granted.")


async def release_pending(box, pending: dict) -> dict:
    """Resume: read the recorded approval rows through MCP; release only when every one says approved.
    The server re-checks them inside the write, so a wrong answer here still cannot release."""
    with get_client().start_as_current_observation(
            name="approval_transition", as_type="span",
            input={"request": pending["request_id"], "approval_ids": pending.get("approval_ids")},
            metadata={"request_id": box.ctx.get("request_id"), "origin_request_id": pending.get("origin_request_id"),
                      "phase": "release"}) as sp:
        aps = [r for r in await box.call("read_approval", {"request_id": pending["request_id"]}) if isinstance(r, dict)]
        status = {a["approval_id"]: a["status"] for a in aps}
        if not aps or any(v != "approved" for v in status.values()):
            sp.update(output={"released": False, "approvals": status, "transition": "none: still waiting"})
            return {"released": False, "approvals": status}
        out = (await box.call("release_access", {"request_id": pending["request_id"], "consent_id": pending["consent_id"]}))[0]
        sp.update(output={"released": True, "approvals": status, "transition": "approved -> released to IT, no seat assigned",
                          "replayed": out.get("replayed", False)})
        return {"released": True, "approvals": status, "replayed": out.get("replayed", False), "request": out.get("entity_id")}
