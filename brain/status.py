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


def answer_agrees(status: str, text: str) -> bool:
    """Code-side check. Nothing in Step 2 can grant or file, so the answer may never say it did."""
    return status in STATUSES and not _GRANT.search(text)
