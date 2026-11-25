"""The Maya -> Webex handoff (spec 6.5). Maya never files: she emits this typed object, and the Webex
side (Step 4) acts on it. Adapted from the lesson 10 homework's WebexPort. Application code creates the
handoff, never the model; the key makes a replay a no-op."""
from typing import Literal, Protocol

from pydantic import BaseModel


class WebexHandoff(BaseModel):
    subject_employee_id: str
    system: str
    justification: str
    conversation_id: str
    caller_id: str
    idempotency_key: str


class PendingAccessResult(BaseModel):
    status: Literal["pending"] = "pending"
    idempotency_key: str
    note: str = "Handed to the Webex workflow. Nothing has been filed, approved or granted."


class WebexPort(Protocol):
    async def request_access(self, handoff: WebexHandoff) -> PendingAccessResult: ...


class PendingWebexPort:
    """Step 3 stand-in: records calls, returns pending, writes nothing. Step 4 replaces it with the real workflow."""

    def __init__(self):
        self.calls: list[WebexHandoff] = []

    async def request_access(self, handoff: WebexHandoff) -> PendingAccessResult:
        self.calls.append(handoff)
        return PendingAccessResult(idempotency_key=handoff.idempotency_key)


def make_key(employee_id: str, system: str, conversation_id: str) -> str:
    return f"{employee_id}:{system.strip().lower()}:{conversation_id}"
