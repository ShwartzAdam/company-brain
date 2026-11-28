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


_NEG = re.compile(r"\b(no|not|never|nothing|none|without|yet|cannot|can't|hasn't|haven't|wasn't|isn't)\b|n't\b", re.I)


def claims_done(text: str) -> list[str]:
    """Sentences that say something was filed or granted. A sentence that also negates it ('No request has been filed
    yet') is the honest outcome, not a claim. ponytail: any negation word exempts the sentence; a parser if that misses."""
    return [x for x in re.split(r"(?<=[.!?])\s+", text) if _GRANT.search(x) and not _NEG.search(x)]


def answer_agrees(status: str, text: str) -> bool:
    """Code-side check. Nothing in Step 2 can grant or file, so the answer may never say it did."""
    return status in STATUSES and not claims_done(text)
