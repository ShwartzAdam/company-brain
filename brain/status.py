"""Terminal status: exactly one of five, and the answer text must agree with it."""
import re

STATUSES = ("completed", "blocked", "pending", "needs_human", "failed")
_GRANT = re.compile(r"(access|seat|license|licence|account)[^.]{0,40}\b(has been|was|is now|is)\s+(granted|provisioned|active|enabled)"
                    r"|\bi(?:'ve| have)\s+(granted|provisioned|filed|created)\b|\bhas been filed\b", re.I)


def final_status(*, error: bool, clarify: bool, refuse: bool, action_unreleased: bool) -> str:
    if error:
        return "failed"
    if clarify or action_unreleased:
        return "pending"
    if refuse:
        return "blocked"
    return "completed"


_NEG = re.compile(r"\b(no|not|never|nothing|none|without|cannot|can't|hasn't|haven't|wasn't|isn't)\b|n't\b", re.I)
_CLAUSE = re.compile(r"[,;:]|\b(?:but|however|although|though)\b", re.I)


def _negated(sentence: str, m: re.Match) -> bool:
    """A negation word earlier in the same clause as the match ('No request has been filed')."""
    return bool(_NEG.search(_CLAUSE.split(sentence[:m.end()])[-1]))


def claims_done(text: str) -> list[str]:
    """Sentences that say something was filed or granted. A match negated earlier in its own clause ('No request has
    been filed yet') is the honest outcome, not a claim; a negation after it or in another clause ('access is now active,
    nothing pending') does not save it. ponytail: clause split on punctuation and 'but'; a parser if that misses."""
    return [x for x in re.split(r"(?<=[.!?])\s+", text) if any(not _negated(x, m) for m in _GRANT.finditer(x))]


def answer_agrees(status: str, text: str) -> bool:
    """Code-side check. Nothing in Step 2 can grant or file, so the answer may never say it did."""
    return status in STATUSES and not claims_done(text)
