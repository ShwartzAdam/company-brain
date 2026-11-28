"""The answer as a typed object: each sentence with the sources that back it (spec 6.5, 'every claim needs a source').
Written by code from the model's text and this turn's evidence: a sentence that names an id the evidence holds (E010,
AR001, T001) gets that row's source attached by code, never by the model. A sentence with a number or id and no source
is counted as uncited, not invented around."""
import re

from pydantic import BaseModel, Field

_ID = re.compile(r"\b[A-Z]{1,3}\d{3}\b")
_CITE = re.compile(r"\[([^\[\]]+)\]")
_CLAIM = re.compile(r"\d|\b[A-Z]{1,3}\d{3}\b")


class Claim(BaseModel):
    text: str
    sources: list[str] = Field(default_factory=list)
    added_by_code: list[str] = Field(default_factory=list)


class AnswerClaims(BaseModel):
    claims: list[Claim]

    @property
    def uncited(self) -> list[str]:
        return [c.text for c in self.claims if not c.sources and _CLAIM.search(c.text)]


def ground(text: str, allowed: set) -> tuple[str, AnswerClaims]:
    """-> (text with code-attached citations, the typed claims). `allowed` = sources of this turn's evidence."""
    by_id = {}
    for s in sorted(allowed):
        if s.startswith("db:") and "/" in s:
            by_id.setdefault(s.rsplit("/", 1)[1], []).append(s)
    out, claims = [], []
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        if not sent:
            continue
        have = [c for c in _CITE.findall(sent) if c in allowed]
        extra = [] if have else [s for i in dict.fromkeys(_ID.findall(sent)) for s in by_id.get(i, [])][:3]
        if extra:  # the sentence names an id the evidence holds: attach that row, before the closing punctuation
            tail = re.search(r"[.!?]+$", sent)
            sent = (sent[:tail.start()] if tail else sent) + "".join(f" [{s}]" for s in extra) + (tail.group(0) if tail else "")
        out.append(sent)
        claims.append(Claim(text=sent, sources=[*have, *extra], added_by_code=extra))
    return " ".join(out), AnswerClaims(claims=claims)
