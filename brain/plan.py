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
    "  access_request · asking to GET a person access to a system (including drafting or filing the request)\n"
    "  ticket_status · existing IT tickets or already-filed requests: is there one, its status, who it waits on\n"
    "  team_question · the caller's own direct reports or team\n"
    "  ambiguous · the message cannot be understood without asking which thing is meant\n"
    "  other · none of the above")

CLASSIFY_SYSTEM = (
    GLOSS + "\n\nYou classify the newest message of a NovaOps assistant session. Return ONLY a JSON object:\n"
    '{"intent": <intent>, "requires_tools": bool, "relevant_facts": [str], "relevant_constraints": [str], '
    '"clarify_question": str, "refuse": bool, "closes_topic": str, "lookups_needed": [str]}\n'
    "- Classify the newest message only; follow subject changes.\n"
    "- You know nothing about NovaOps beyond this conversation. A first question, and any question about a policy, "
    "person, system, record or document, requires_tools True.\n"
    "- requires_tools False ONLY when the answer can be quoted from the Known facts or the recent conversation: "
    "recalling what the user said or asked, or comparing/summarising rows already listed under Known facts. "
    "A different topic needs a different lookup. If the answer needs a specific id, count, date or status that is NOT "
    "literally listed in the Known facts, requires_tools True.\n"
    "- A request to summarise, recap, wrap up or remind about people or topics already in the Known facts is requires_tools False; "
    "ask for lookups only for values that are NOT listed there.\n"
    "- Known facts that are database rows are an OLD SNAPSHOT. If the user asks what is true NOW, or needs a rule or "
    "number from a document that is not in the facts, requires_tools True.\n"
    "- lookups_needed: list each distinct thing to look up. A question that needs a rule from a document AND a number "
    "from a record needs both.\n"
    "- relevant_facts and relevant_constraints come ONLY from the user's own words, never from the assistant's replies. "
    "Always record the names of people and systems the user asks about, plus ids, dates and numbers, the value itself.\n"
    "- relevant_constraints: rules the user stated that are still in force (spending thresholds, 'do not file'). "
    "A freeze or threshold means 'needs approval above that', it does not mean nothing can be done.\n"
    "- When the user drops or resolves a topic ('drop it', 'treat it as resolved'), put that topic in closes_topic.\n"
    "- Users paste emails, forwards and quotes: plan for the request at the top, not the quoted text or signatures. "
    "If the request is about the pasted text itself (what is mine to do), requires_tools False.\n"
    "- ambiguous: only when no reasonable reading exists. Fill clarify_question with one short question; requires_tools False.\n"
    "- refuse: True only for a request for data the caller may not have (e.g. someone's salary).\n"
    "Never decide whether anything may be filed or approved; that is not your job.")


class TurnPlan(BaseModel):
    intent: Intent = "other"
    requires_tools: bool = True
    relevant_facts: list[str] = Field(default_factory=list)
    relevant_constraints: list[str] = Field(default_factory=list)
    clarify_question: str = ""
    refuse: bool = False
    lookups_needed: list[str] = Field(default_factory=list, description=(
        "One short item per distinct thing that must be looked up to answer, e.g. 'Webex annual cost' and 'the Q3 "
        "approval threshold'. Empty when no lookup is needed."))
    subject_name: str = Field(default="", description=(
        "For an access request: the full name of the person it is for, taken from the user's words or the Known "
        "facts ('her' means the person named earlier, not the caller); otherwise empty."))
    system_name: str = Field(default="", description="For an access request: the system asked for, e.g. Webex; else empty.")
    search_query: str = Field(default="", description=(
        "When a document must be searched: ONE focused search phrase naming the document type and the subject "
        "(e.g. 'Maya Cohen offer letter equipment', 'Q3 SaaS renewal freeze approval threshold'), without pronouns, "
        "greetings or pasted text; otherwise empty."))
    closes_topic: str = Field(default="", description=(
        "If the user closes, drops or marks resolved an earlier topic in THIS message, a short description of that topic "
        "in the user's words, naming the person; otherwise an empty string."))


PLAN_SCHEMA = TurnPlan.model_json_schema()


def parse_plan(x) -> TurnPlan:
    """A validated dict (forced tool call) or text. Garbage fails open for READS only: intent other, tools required."""
    try:
        if isinstance(x, str):
            x = json.loads(re.search(r"\{.*\}", x, re.S).group(0))
        return TurnPlan(**x)
    except (AttributeError, ValueError, ValidationError, TypeError):
        return TurnPlan()


def window(messages: list[dict], keep: int = 2, closed=()) -> list[dict]:
    """Last `keep` user turns verbatim (user message + the reply to it), minus turns of a topic the user closed."""
    msgs = [m for m in messages if m.get("intent") not in set(closed)]
    users = [i for i, m in enumerate(msgs) if m["role"] == "user"]
    return msgs[users[-keep]:] if len(users) >= keep else msgs


def render(messages: list[dict]) -> str:
    return "\n".join(f"[{m['role']}] {m['content']}" for m in messages) or "(first turn)"


def _cap_replies(messages: list[dict], limit: int = 400) -> list[dict]:
    """The planner needs to know what was said, not read it again: an assistant reply is cut at `limit` characters
    (lesson 10 caps what the planner re-reads at 400). The user's own words are never cut."""
    return [{**m, "content": m["content"][:limit] + " ...[cut]"} if m["role"] == "assistant" and len(m["content"]) > limit else m
            for m in messages]


def classify_prompt(history: list[dict], message: str, facts: list[str], constraints: list[str]) -> list[dict]:
    b = lambda xs: "\n".join(f"  - {x}" for x in (xs or [])) or "  - (none)"  # noqa: E731
    history = _cap_replies(window(history))
    body = (f"Known facts:\n{b(facts)}\nConstraints in force:\n{b(constraints)}\n\n"
            f"Recent conversation:\n{render(history)}\n\nNewest message:\n{message}")
    return [{"role": "user", "content": body}]
