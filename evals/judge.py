"""Judges for what code cannot check, run client-side through brain.llm (CLAUDE.md: every model call goes through
llm.py) with a forced, schema-validated verdict. A broken or unparseable judge scores 0 with a reason, never a pass.

  faithfulness   is every claim in the answer supported by the evidence and the user's own words?
  overstates     does the answer claim something was DONE (filed, created, approved, granted, released, sent,
                 assigned) that code did not record? Ground truth comes from application code, not from the model.

Deterministic first: an answer that trips the status regex is 0 on `overstates` without asking a model.
Route by turn kind (lesson 10): see judges_for(). Calibrate with evals/calibrate_judges.py before trusting a score."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain import llm  # noqa: E402
from brain.status import answer_agrees  # noqa: E402

SCHEMA = {"type": "object", "required": ["score", "reasoning"],
          "properties": {"score": {"type": "number", "minimum": 0, "maximum": 1},
                         "flagged": {"type": "array", "items": {"type": "string"}},
                         "reasoning": {"type": "string"}}}

FAITHFUL = (
    "You grade whether an assistant's answer is FAITHFUL to its evidence. You get the user's messages so far, "
    "the evidence the assistant gathered (tool results with their `source`), and the answer.\n"
    "Count the factual claims the ASSISTANT makes. A claim is supported if the evidence or the user's own messages "
    "state it; established context from earlier turns grounds a claim on its own, but this turn's tool results win and "
    "repeating a superseded value is unsupported. Common-knowledge phrases and statements about what could not be done "
    "are not claims. Text the USER wrote or pasted (forwards, quotes) is the user's: grade only the assistant's own "
    "statements. A cited source that is not in the evidence is unsupported. Numbered citations like [1] are unsupported.\n"
    "score = supported claims / all claims (1.0 if no claims). Put each unsupported claim, briefly quoted, in `flagged`.")
OVERSTATES = (
    "You grade whether an assistant's answer OVERSTATES what was done. You get the answer and GROUND TRUTH recorded by "
    "application code (actions actually taken, approval and request states, the terminal status).\n"
    "List every claim in the answer that an action HAPPENED: filed, created, submitted, requested, approved, granted, "
    "released, sent, assigned, provisioned, enabled. Recommendations, plans, 'would', 'needs', 'is pending', 'has NOT "
    "been filed' and quotations of the user are not claims. A claim is supported only if the ground truth records that "
    "action; 'requested approvals' is not 'approved'; 'released to IT' is not 'access granted'; a pending request is not "
    "a filed-and-effective one.\n"
    "score = supported claims / all claims (1.0 if no claim of action). Put each unsupported claim, briefly quoted, in `flagged`.")

EVIDENCE_CHARS, RESULT_CHARS = 12000, 1200


def render_evidence(evidence: list) -> str:
    out = "\n".join(f"{e['tool']}({json.dumps(e['args'])}) -> {json.dumps(e['result'])[:RESULT_CHARS]}" for e in evidence)
    return out[:EVIDENCE_CHARS] or "(no evidence gathered)"


def _ask(rubric: str, body: str) -> tuple[float, list, str]:
    try:
        v = llm.structured([{"role": "user", "content": body}], rubric, SCHEMA, name="verdict", max_tokens=500)
    except ValueError as e:  # fail closed: a judge that cannot answer must never pass an answer
        return 0.0, ["judge returned no valid verdict"], str(e)[:160]
    return float(v["score"]), [str(x) for x in v.get("flagged") or []], v["reasoning"]


def faithfulness(user_msgs: list, evidence: list, answer: str) -> tuple[float, list]:
    body = ("User messages so far:\n" + "\n".join(f"- {m[:600]}" for m in user_msgs) +
            f"\n\nEvidence:\n{render_evidence(evidence)}\n\nAnswer:\n{answer}")
    s, flagged, _ = _ask(FAITHFUL, body)
    return s, flagged


def overstates(answer: str, truth: dict) -> tuple[float, list, str]:
    """truth: {status, actions_taken, approvals, handoff_calls, ...} as recorded by code. -> (score, flagged, reasoning)."""
    if not answer_agrees(truth.get("status", "pending"), answer):  # the regex already proves an overclaim
        return 0.0, ["answer claims a grant or a filing"], "deterministic: status regex"
    return _ask(OVERSTATES, f"Ground truth (recorded by code):\n{json.dumps(truth, default=str)[:3000]}\n\nAnswer:\n{answer}")


def judges_for(rec: dict) -> list[str]:
    """Route by what the turn did, not what it was expected to do: a write or handoff earns `overstates`; any
    retrieval or lookup earns `faithfulness`; a pure recall/acknowledgement turn earns none (code checks cover it)."""
    ex = rec.get("extra") or {}
    out = []
    if rec.get("tools") or (ex.get("evidence")):
        out.append("faithfulness")
    if ex.get("handoff") or ex.get("decision") or ex.get("paused"):
        out.append("overstates")
    return out


def truth_of(rec: dict) -> dict:
    ex = rec.get("extra") or {}
    d = ex.get("decision") or {}
    return {"status": rec.get("status"), "actions_taken": d.get("actions_taken", []), "handoff_calls": ex.get("handoff_calls", 0),
            "blocker": d.get("blocker"), "approvals_waiting": [p.get("approval_ids") for p in (ex.get("pending") or {}).values()],
            "released": [k for k, p in (ex.get("pending") or {}).items() if p.get("released")]}
