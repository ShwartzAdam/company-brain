"""Classify step: one model call that labels the newest message. It IS the router.
Adapted from ai-course lesson-10 stage01_policy.py (TurnPlan, planner prompt, window)."""
import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

Intent = Literal["policy_question", "employee_lookup", "onboarding_status", "equipment_request",
                 "subscription_review", "access_request", "ticket_status", "team_question",
                 "ambiguous", "other"]

GLOSS = (
    "Intent = the TOPIC of the newest message:\n"
    "  policy_question · what a policy, handbook or IT knowledge-base article says\n"
    "  employee_lookup · who someone is (record, role, manager, status)\n"
    "  onboarding_status · a joiner's checklist, required systems, paperwork, eligibility\n"
    "  equipment_request · hardware (laptops, monitors, docks, headsets)\n"
    "  subscription_review · a SaaS subscription's seats, cost, contract or renewal\n"
    "  access_request · getting a person access to a system (including drafting or filing the request)\n"
    "  ticket_status · existing IT tickets\n"
    "  team_question · the caller's own direct reports or team\n"
    "  ambiguous · the message cannot be understood without asking which thing is meant\n"
    "  other · none of the above")

CLASSIFY_SYSTEM = (
    GLOSS + "\n\nYou classify the newest message of a NovaOps assistant session. Return ONLY a JSON object:\n"
    '{"intent": <intent>, "requires_tools": bool, "relevant_facts": [str], "relevant_constraints": [str], '
    '"clarify_question": str, "refuse": bool}\n'
    "- Classify the newest message only; follow subject changes.\n"
    "- relevant_facts: values from earlier turns later answers need (ids, dates, numbers, names), the value itself.\n"
    "- relevant_constraints: rules the user stated that are still in force (spending thresholds, 'do not file').\n"
    "- requires_tools is False only for recaps and reasoning over facts already in this conversation; a new "
    "topic or a first question is True. Drafting text is not a tool need.\n"
    "- Users paste emails, forwards and quotes: classify the request at the top, not the quoted text.\n"
    "- ambiguous: fill clarify_question with one short question; requires_tools False.\n"
    "- refuse: True only for a request for data the caller may not have (e.g. someone's salary).\n"
    "Never decide whether anything may be filed or approved; that is not your job.")


class TurnPlan(BaseModel):
    intent: Intent = "other"
    requires_tools: bool = True
    relevant_facts: list[str] = Field(default_factory=list)
    relevant_constraints: list[str] = Field(default_factory=list)
    clarify_question: str = ""
    refuse: bool = False


def parse_plan(text: str) -> TurnPlan:
    """Tolerant parse. Garbage fails open for READS only: intent other, tools required."""
    m = re.search(r"\{.*\}", text, re.S)
    try:
        return TurnPlan(**json.loads(m.group(0)))
    except (AttributeError, ValueError, ValidationError, TypeError):
        return TurnPlan()


def window(messages: list[dict], keep: int = 2) -> list[dict]:
    """Last `keep` user turns verbatim (user message + the reply to it)."""
    users = [i for i, m in enumerate(messages) if m["role"] == "user"]
    return messages[users[-keep]:] if len(users) >= keep else list(messages)


def render(messages: list[dict]) -> str:
    return "\n".join(f"[{m['role']}] {m['content']}" for m in messages) or "(first turn)"


def classify_prompt(history: list[dict], message: str, facts: list[str], constraints: list[str]) -> list[dict]:
    b = lambda xs: "\n".join(f"  - {x}" for x in (xs or [])) or "  - (none)"  # noqa: E731
    body = (f"Known facts:\n{b(facts)}\nConstraints in force:\n{b(constraints)}\n\n"
            f"Recent conversation:\n{render(window(history))}\n\nNewest message:\n{message}")
    return [{"role": "user", "content": body}]
