"""Faithfulness judge (lesson 10/11 pattern): one model call, rubric as system prompt, JSON verdict back.
Used only where code cannot decide: is every claim in the answer supported by what the session gathered?
Judged against ALL evidence and the user's own messages so far (session_faithfulness), because a wrong
claim often surfaces turns after the lookup that should have grounded it. Goes through brain.llm."""
import json
import re

from brain import llm

RUBRIC = (
    "You grade whether an assistant's answer is FAITHFUL to its evidence. You get the user's messages so far, "
    "the evidence the assistant gathered (tool results with their `source`), and the answer.\n"
    "A claim is supported if the evidence or the user's own messages state it. Common knowledge phrases and "
    "statements about what could not be done are not claims. Text the USER wrote or pasted (including forwarded emails and "
    "quoted questions) is the user's, not an assistant claim: grade only the assistant's own statements.  A cited source that is not in the evidence is "
    "unsupported. Numbered citations like [1] are unsupported.\n"
    'Return ONLY JSON: {"score": <0.0-1.0, the fraction of factual claims supported>, '
    '"unsupported": [<each unsupported claim, quoted briefly>]}')
EVIDENCE_CHARS, RESULT_CHARS = 12000, 1200


def render_evidence(evidence: list) -> str:
    out = "\n".join(f"{e['tool']}({json.dumps(e['args'])}) -> {json.dumps(e['result'])[:RESULT_CHARS]}" for e in evidence)
    return out[:EVIDENCE_CHARS] or "(no evidence gathered)"


def parse_verdict(text: str) -> tuple[float, list]:
    """Tolerant. Garbage scores 0.0 so a broken judge can never silently pass an answer."""
    m = re.search(r"\{.*\}", text, re.S)
    try:
        d = json.loads(m.group(0))
        return min(1.0, max(0.0, float(d["score"]))), [str(x) for x in d.get("unsupported") or []]
    except (AttributeError, ValueError, KeyError, TypeError):
        return 0.0, ["judge returned no verdict"]


def faithfulness(user_msgs: list, evidence: list, answer: str) -> tuple[float, list]:
    body = ("User messages so far:\n" + "\n".join(f"- {m[:600]}" for m in user_msgs) +
            f"\n\nEvidence:\n{render_evidence(evidence)}\n\nAnswer:\n{answer}")
    return parse_verdict(llm.complete([{"role": "user", "content": body}], RUBRIC, max_tokens=500, temperature=0))
